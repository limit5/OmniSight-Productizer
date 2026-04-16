"""P3 (#288) — Code-signing chain management (extends ``secret_store``).

Bundles five concerns into one cohesive module so the signing surface
stays inspectable end-to-end:

  1. **Apple certs** — Developer ID + Provisioning Profile + App Store
     Distribution Certificate.  Each stored with ``Fernet`` at-rest
     encryption via :mod:`backend.secret_store`; only the certificate
     **fingerprint** (SHA-256 hex, last-12) appears in UI / logs.
  2. **Android keystore** — per-app JKS/PKCS12 keystore, alias, plus
     keystore and key passwords.  Encrypted artefact + passwords live
     behind the same Fernet key as Apple certs.
  3. **HSM integration (optional)** — AWS KMS / GCP KMS / YubiHSM.  The
     module never materialises an HSM-backed private key on disk: the
     cert record stores only the vendor-specific ``key_ref`` (ARN,
     resource name, slot id); the signing transport (out of scope for
     P3) issues a vendor API call with the ref.  ``hsm_vendor="none"``
     keeps the encrypted-PEM path for small deploys.
  4. **Sign audit** — :class:`CodeSignAuditChain` writes one
     ``SHA-256(prev_hash || canonical(record))`` entry per sign call.
     Mirrors :class:`backend.security_hardening.MergerVoteAuditChain`
     semantics (in-memory chain + fire-and-forget ``backend.audit``
     persistence) so tests can verify tamper detection without a DB.
  5. **Expiry SSE alerts** — :func:`check_cert_expiries` walks all
     registered certs and fires ``cert_expiry`` events at 30/7/1 day
     thresholds via :mod:`backend.events`.  Each alert severity maps
     to an SSE level (``notice``/``warn``/``critical``) so operator
     dashboards can surface them in REPORTER VORTEX.

All helpers in the cert store layer are pure (no network, no HSM
round-trip) — production signing transports bind the resolved
``CodeSignContext`` to whichever vendor SDK the environment provides.
This keeps tests offline and lets the mobile pipeline (P1 #286 /
P2 #287 / P5 #290) consume the contract without spinning up KMS.

The file-backed JSON index lives at ``data/codesign_store.json``
(mode ``0o600``) so in-process singleton reloads survive a restart.
"""

from __future__ import annotations

import base64
import enum
import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from backend import secret_store

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_STORE_PATH = _PROJECT_ROOT / "data" / "codesign_store.json"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  0. Exceptions + enums
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class CodesignError(Exception):
    """Base class for codesign_store failures."""


class UnknownCertError(CodesignError):
    pass


class DuplicateCertError(CodesignError):
    pass


class InvalidCertError(CodesignError):
    pass


class UnknownHSMVendorError(CodesignError):
    pass


class InvalidHSMKeyRefError(CodesignError):
    pass


class SigningChainError(CodesignError):
    pass


class CertKind(str, enum.Enum):
    apple_developer_id = "apple_developer_id"
    apple_provisioning_profile = "apple_provisioning_profile"
    apple_app_store_distribution = "apple_app_store_distribution"
    android_keystore = "android_keystore"


APPLE_CERT_KINDS: frozenset[CertKind] = frozenset({
    CertKind.apple_developer_id,
    CertKind.apple_app_store_distribution,
})


class HSMVendor(str, enum.Enum):
    """Optional HSM providers.  ``none`` = software-only (Fernet at-rest)."""

    none = "none"
    aws_kms = "aws_kms"
    gcp_kms = "gcp_kms"
    yubihsm = "yubihsm"


SUPPORTED_HSM_VENDORS: frozenset[HSMVendor] = frozenset(HSMVendor)

# Pre-expiry alert thresholds (days).  Ordered ascending so callers can
# iterate and the first-matching threshold (smallest) wins.
EXPIRY_THRESHOLD_DAYS: tuple[int, ...] = (1, 7, 30)

_SEVERITY_BY_THRESHOLD: dict[int, str] = {
    1: "critical",
    7: "warn",
    30: "notice",
}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  1. HSM layer
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


# Vendor-specific regex for ``key_ref`` shape validation.  We never
# round-trip to the vendor (P3 is offline) — shape check catches typos
# at store-time so the later transport does not fail mid-sign.
_HSM_KEY_REF_PATTERNS: dict[HSMVendor, re.Pattern[str]] = {
    HSMVendor.aws_kms: re.compile(
        r"^arn:aws(?:-[a-z-]+)?:kms:[a-z0-9-]+:\d{12}:key/"
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    ),
    HSMVendor.gcp_kms: re.compile(
        r"^projects/[a-z0-9-]+/locations/[a-z0-9-]+/"
        r"keyRings/[A-Za-z0-9_-]+/cryptoKeys/[A-Za-z0-9_-]+"
        r"(?:/cryptoKeyVersions/\d+)?$",
    ),
    HSMVendor.yubihsm: re.compile(
        r"^yubihsm://(?P<serial>\d+)/slot/(?P<slot>\d{1,4})"
        r"(?:#label=[A-Za-z0-9_.-]+)?$",
    ),
}


@dataclass(frozen=True)
class HSMProvider:
    """Describes an HSM-backed key reference.

    Opaque value-type — ``describe()`` is always safe to log (no secret
    value ever enters the object, only references / identifiers).  The
    signing transport turns this into a vendor API call.
    """

    vendor: HSMVendor
    key_ref: str

    def describe(self) -> dict[str, str]:
        return {
            "vendor": self.vendor.value,
            "key_ref_fingerprint": _ref_fingerprint(self.key_ref),
        }


def validate_hsm_key_ref(vendor: HSMVendor, key_ref: str) -> None:
    """Raise :class:`InvalidHSMKeyRefError` if the key_ref shape is wrong.

    ``vendor="none"`` always accepts empty ``key_ref`` and rejects a
    non-empty one (since a software cert has no HSM ref).
    """
    if vendor is HSMVendor.none:
        if key_ref:
            raise InvalidHSMKeyRefError(
                "hsm_vendor=none must have empty key_ref",
            )
        return
    pattern = _HSM_KEY_REF_PATTERNS.get(vendor)
    if pattern is None:
        raise UnknownHSMVendorError(f"unsupported HSM vendor: {vendor}")
    if not pattern.match(key_ref or ""):
        raise InvalidHSMKeyRefError(
            f"key_ref does not match {vendor.value} format: {_ref_fingerprint(key_ref)}",
        )


def resolve_hsm_provider(vendor: str | HSMVendor, key_ref: str) -> HSMProvider:
    """Build an :class:`HSMProvider`, validating shape up front."""
    v = _coerce_vendor(vendor)
    validate_hsm_key_ref(v, key_ref)
    return HSMProvider(vendor=v, key_ref=key_ref)


def _coerce_vendor(vendor: str | HSMVendor) -> HSMVendor:
    if isinstance(vendor, HSMVendor):
        return vendor
    try:
        return HSMVendor(vendor.strip().lower())
    except ValueError as exc:
        raise UnknownHSMVendorError(
            f"unknown HSM vendor {vendor!r}; supported: "
            f"{sorted(v.value for v in HSMVendor)}",
        ) from exc


def _ref_fingerprint(ref: str) -> str:
    """Last-12 SHA-256 hex of a reference string, for log-safe display."""
    if not ref:
        return "(unset)"
    h = hashlib.sha256(ref.encode("utf-8")).hexdigest()
    return f"sha256:{h[-12:]}"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  2. Cert records (Apple + Android)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass(frozen=True)
class CertRecord:
    """Common shape for every stored signing artefact.

    Only ``encrypted_material`` carries secret bytes (Fernet ciphertext
    of the PEM / provisioning profile / keystore blob).  Extra vendor
    fields travel in ``extra``; they are sanitised by
    :func:`redacted_view` before appearing in listings / SSE / logs.
    """

    cert_id: str
    kind: CertKind
    subject_cn: str
    serial: str
    team_id: str
    not_before: float
    not_after: float
    fingerprint_sha256: str
    hsm_vendor: HSMVendor
    hsm_key_ref: str  # "" when hsm_vendor=none
    encrypted_material: str  # base64(Fernet(...)) ; "" when HSM-backed
    extra: dict[str, Any] = field(default_factory=dict)

    def days_until_expiry(self, *, now: float | None = None) -> float:
        now_v = float(now) if now is not None else time.time()
        return (self.not_after - now_v) / 86400.0

    def is_expired(self, *, now: float | None = None) -> bool:
        return self.days_until_expiry(now=now) <= 0


def redacted_view(record: CertRecord) -> dict[str, Any]:
    """Return a log-safe / UI-safe dict view (no secret bytes)."""
    return {
        "cert_id": record.cert_id,
        "kind": record.kind.value,
        "subject_cn": record.subject_cn,
        "serial": record.serial,
        "team_id": record.team_id,
        "not_before": record.not_before,
        "not_after": record.not_after,
        "fingerprint_sha256": record.fingerprint_sha256,
        "hsm_vendor": record.hsm_vendor.value,
        "hsm_key_ref_fingerprint": _ref_fingerprint(record.hsm_key_ref),
        "has_encrypted_material": bool(record.encrypted_material),
        "extra": _redact_extra(record.extra),
    }


_REDACT_KEYS: frozenset[str] = frozenset({
    "password", "keystore_password", "key_password",
    "pem", "private_key", "passphrase", "secret",
})


def _redact_extra(d: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in d.items():
        if k.lower() in _REDACT_KEYS:
            out[k] = "(redacted)"
        else:
            out[k] = v
    return out


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  3. Store (in-memory singleton + JSON file backing)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class CodesignStore:
    """In-process cert registry backed by a JSON index on disk.

    Thread-safety: single-writer contract — callers serialise at the
    orchestration layer (store mutations happen only on the signing
    coordinator's queue).  Reads are lock-free (copy-on-write list).
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = Path(path) if path is not None else _STORE_PATH
        self._records: dict[str, CertRecord] = {}
        self._load()

    # ───────── public API ─────────

    def register_apple_cert(
        self,
        *,
        cert_id: str,
        kind: CertKind,
        team_id: str,
        subject_cn: str,
        serial: str,
        not_before: float,
        not_after: float,
        pem_bytes: bytes | None = None,
        hsm_vendor: str | HSMVendor = HSMVendor.none,
        hsm_key_ref: str = "",
        extra: dict[str, Any] | None = None,
    ) -> CertRecord:
        if kind not in APPLE_CERT_KINDS:
            raise InvalidCertError(
                f"register_apple_cert requires an Apple cert kind, got {kind}",
            )
        vendor = _coerce_vendor(hsm_vendor)
        validate_hsm_key_ref(vendor, hsm_key_ref)
        _require_team_id(team_id)
        _require_validity_window(not_before, not_after)
        encrypted = _encrypt_material(vendor, pem_bytes)
        fingerprint = _material_fingerprint(pem_bytes, serial=serial)
        record = CertRecord(
            cert_id=cert_id,
            kind=kind,
            subject_cn=subject_cn,
            serial=serial,
            team_id=team_id,
            not_before=float(not_before),
            not_after=float(not_after),
            fingerprint_sha256=fingerprint,
            hsm_vendor=vendor,
            hsm_key_ref=hsm_key_ref,
            encrypted_material=encrypted,
            extra=dict(extra or {}),
        )
        self._insert(record)
        return record

    def register_provisioning_profile(
        self,
        *,
        cert_id: str,
        team_id: str,
        app_id: str,
        profile_uuid: str,
        profile_bytes: bytes,
        not_before: float,
        not_after: float,
        associated_cert_id: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> CertRecord:
        _require_team_id(team_id)
        _require_validity_window(not_before, not_after)
        if not app_id:
            raise InvalidCertError("app_id required for provisioning profile")
        if not profile_uuid:
            raise InvalidCertError("profile_uuid required")
        merged_extra = {
            "app_id": app_id,
            "profile_uuid": profile_uuid,
            "associated_cert_id": associated_cert_id or "",
            **(extra or {}),
        }
        encrypted = _encrypt_material(HSMVendor.none, profile_bytes)
        fingerprint = _material_fingerprint(profile_bytes, serial=profile_uuid)
        record = CertRecord(
            cert_id=cert_id,
            kind=CertKind.apple_provisioning_profile,
            subject_cn=f"profile:{app_id}",
            serial=profile_uuid,
            team_id=team_id,
            not_before=float(not_before),
            not_after=float(not_after),
            fingerprint_sha256=fingerprint,
            hsm_vendor=HSMVendor.none,
            hsm_key_ref="",
            encrypted_material=encrypted,
            extra=merged_extra,
        )
        self._insert(record)
        return record

    def register_android_keystore(
        self,
        *,
        cert_id: str,
        package_name: str,
        alias: str,
        keystore_bytes: bytes | None,
        keystore_password: str | None,
        key_password: str | None,
        subject_cn: str,
        not_before: float,
        not_after: float,
        hsm_vendor: str | HSMVendor = HSMVendor.none,
        hsm_key_ref: str = "",
        extra: dict[str, Any] | None = None,
    ) -> CertRecord:
        if not package_name:
            raise InvalidCertError("package_name required for android keystore")
        if not alias:
            raise InvalidCertError("alias required for android keystore")
        vendor = _coerce_vendor(hsm_vendor)
        validate_hsm_key_ref(vendor, hsm_key_ref)
        _require_validity_window(not_before, not_after)
        if vendor is HSMVendor.none:
            if not keystore_bytes:
                raise InvalidCertError(
                    "keystore_bytes required when hsm_vendor=none",
                )
            if not keystore_password or not key_password:
                raise InvalidCertError(
                    "keystore_password + key_password required when hsm_vendor=none",
                )
        encrypted_store = _encrypt_material(vendor, keystore_bytes)
        encrypted_ks_pw = (
            secret_store.encrypt(keystore_password)
            if keystore_password
            else ""
        )
        encrypted_key_pw = (
            secret_store.encrypt(key_password) if key_password else ""
        )
        fingerprint = _material_fingerprint(
            keystore_bytes, serial=f"{package_name}:{alias}",
        )
        merged_extra = {
            "package_name": package_name,
            "alias": alias,
            "encrypted_keystore_password": encrypted_ks_pw,
            "encrypted_key_password": encrypted_key_pw,
            **(extra or {}),
        }
        record = CertRecord(
            cert_id=cert_id,
            kind=CertKind.android_keystore,
            subject_cn=subject_cn,
            serial=f"{package_name}:{alias}",
            team_id="",  # Android has no Apple-style Team ID
            not_before=float(not_before),
            not_after=float(not_after),
            fingerprint_sha256=fingerprint,
            hsm_vendor=vendor,
            hsm_key_ref=hsm_key_ref,
            encrypted_material=encrypted_store,
            extra=merged_extra,
        )
        self._insert(record)
        return record

    def get(self, cert_id: str) -> CertRecord:
        rec = self._records.get(cert_id)
        if rec is None:
            raise UnknownCertError(f"unknown cert_id: {cert_id}")
        return rec

    def delete(self, cert_id: str) -> bool:
        removed = self._records.pop(cert_id, None) is not None
        if removed:
            self._persist()
        return removed

    def list_records(self) -> list[CertRecord]:
        return list(self._records.values())

    def list_redacted(self) -> list[dict[str, Any]]:
        return [redacted_view(r) for r in self._records.values()]

    def decrypt_material(self, cert_id: str) -> bytes:
        """Return the plaintext artefact bytes.  Raises when HSM-backed
        or when no material is stored."""
        rec = self.get(cert_id)
        if rec.hsm_vendor is not HSMVendor.none:
            raise SigningChainError(
                f"{cert_id} is HSM-backed ({rec.hsm_vendor.value}); "
                "private key never leaves the HSM",
            )
        if not rec.encrypted_material:
            raise SigningChainError(f"{cert_id} has no encrypted material")
        raw_b64 = secret_store.decrypt(rec.encrypted_material)
        return base64.b64decode(raw_b64)

    def decrypt_android_passwords(self, cert_id: str) -> tuple[str, str]:
        """Return ``(keystore_password, key_password)`` plaintext."""
        rec = self.get(cert_id)
        if rec.kind is not CertKind.android_keystore:
            raise SigningChainError(
                f"{cert_id} is not an android_keystore (kind={rec.kind.value})",
            )
        ks_enc = rec.extra.get("encrypted_keystore_password") or ""
        k_enc = rec.extra.get("encrypted_key_password") or ""
        if not ks_enc or not k_enc:
            raise SigningChainError(
                f"{cert_id} missing encrypted passwords (HSM-only?)",
            )
        return (secret_store.decrypt(ks_enc), secret_store.decrypt(k_enc))

    # ───────── internals ─────────

    def _insert(self, record: CertRecord) -> None:
        if record.cert_id in self._records:
            raise DuplicateCertError(
                f"cert_id already registered: {record.cert_id}",
            )
        if not record.cert_id or not re.match(r"^[A-Za-z0-9_.:-]+$", record.cert_id):
            raise InvalidCertError(
                f"invalid cert_id: {record.cert_id!r}",
            )
        self._records[record.cert_id] = record
        self._persist()

    def _persist(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            cid: _record_to_json(r) for cid, r in self._records.items()
        }
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, sort_keys=True, indent=2))
        try:
            tmp.chmod(0o600)
        except OSError:  # pragma: no cover — Windows / exotic FS
            pass
        os.replace(tmp, self._path)
        try:
            self._path.chmod(0o600)
        except OSError:  # pragma: no cover
            pass

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("codesign store at %s unreadable: %s", self._path, exc)
            return
        for cid, payload in raw.items():
            try:
                self._records[cid] = _record_from_json(payload)
            except Exception as exc:  # pragma: no cover — corrupt entries
                logger.warning("skipping corrupt cert %s: %s", cid, exc)


def _record_to_json(r: CertRecord) -> dict[str, Any]:
    return {
        "cert_id": r.cert_id,
        "kind": r.kind.value,
        "subject_cn": r.subject_cn,
        "serial": r.serial,
        "team_id": r.team_id,
        "not_before": r.not_before,
        "not_after": r.not_after,
        "fingerprint_sha256": r.fingerprint_sha256,
        "hsm_vendor": r.hsm_vendor.value,
        "hsm_key_ref": r.hsm_key_ref,
        "encrypted_material": r.encrypted_material,
        "extra": r.extra,
    }


def _record_from_json(p: dict[str, Any]) -> CertRecord:
    return CertRecord(
        cert_id=p["cert_id"],
        kind=CertKind(p["kind"]),
        subject_cn=p.get("subject_cn", ""),
        serial=p.get("serial", ""),
        team_id=p.get("team_id", ""),
        not_before=float(p["not_before"]),
        not_after=float(p["not_after"]),
        fingerprint_sha256=p.get("fingerprint_sha256", ""),
        hsm_vendor=HSMVendor(p.get("hsm_vendor", "none")),
        hsm_key_ref=p.get("hsm_key_ref", ""),
        encrypted_material=p.get("encrypted_material", ""),
        extra=dict(p.get("extra") or {}),
    )


def _require_team_id(team_id: str) -> None:
    if not team_id or not re.match(r"^[A-Z0-9]{6,12}$", team_id):
        raise InvalidCertError(
            f"team_id must be 6-12 alphanumerics (Apple Team ID shape): {team_id!r}",
        )


def _require_validity_window(not_before: float, not_after: float) -> None:
    if not_after <= not_before:
        raise InvalidCertError(
            f"not_after ({not_after}) must be > not_before ({not_before})",
        )


def _encrypt_material(vendor: HSMVendor, raw: bytes | None) -> str:
    if vendor is not HSMVendor.none:
        return ""  # HSM-backed — never store key material locally
    if not raw:
        return ""
    return secret_store.encrypt(base64.b64encode(raw).decode("ascii"))


def _material_fingerprint(raw: bytes | None, *, serial: str) -> str:
    """SHA-256 hex of (serial || raw bytes).  Stable across restarts.

    Using the serial as a salt keeps distinct certs with the same raw
    bytes (edge case) distinguishable, and keeps HSM-backed certs (no
    raw bytes) uniquely fingerprintable by their serial alone.
    """
    h = hashlib.sha256()
    h.update(serial.encode("utf-8"))
    h.update(b"\x00")
    if raw:
        h.update(raw)
    return h.hexdigest()


_store_singleton: CodesignStore | None = None


def get_store() -> CodesignStore:
    global _store_singleton
    if _store_singleton is None:
        _store_singleton = CodesignStore()
    return _store_singleton


def _reset_for_tests(path: Path | None = None) -> None:
    """Test-only: swap the singleton for a scratch instance.

    Pass ``path`` to pin the backing file; omit to use the default
    module path (callers should clean up themselves in that case).
    """
    global _store_singleton
    _store_singleton = CodesignStore(path=path) if path is not None else None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  4. Hash-chain sign audit
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


CODESIGN_AUDIT_ENTITY_KIND = "codesign_chain_sign"


def _sign_canonical(record: dict[str, Any]) -> str:
    return json.dumps(
        record,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def _sign_chain_hash(prev: str, record: dict[str, Any]) -> str:
    return hashlib.sha256(
        (prev + _sign_canonical(record)).encode("utf-8"),
    ).hexdigest()


@dataclass
class CodeSignAuditChain:
    """Hash-chain log of per-artifact sign operations.

    Each entry records *who* signed *what artifact* with *which cert*
    and *when*, chained by ``SHA-256(prev || canonical(record))`` so a
    tamper on any historical row invalidates every subsequent
    ``curr_hash``.  Mirrors :class:`MergerVoteAuditChain` semantics so
    the dashboard can render both chains with the same widget.
    """

    entries: list[dict[str, Any]] = field(default_factory=list)
    persist: bool = True

    def append(
        self,
        *,
        cert_id: str,
        cert_fingerprint: str,
        artifact_path: str,
        artifact_sha256: str,
        actor: str,
        hsm_vendor: str,
        reason_code: str = "sign",
        ts: float | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not cert_id:
            raise ValueError("cert_id required")
        if not artifact_sha256 or not re.match(r"^[0-9a-f]{64}$", artifact_sha256):
            raise ValueError(
                f"artifact_sha256 must be 64 hex chars: {artifact_sha256!r}",
            )
        if not actor:
            raise ValueError("actor required")
        ts_v = float(ts) if ts is not None else time.time()
        record = {
            "cert_id": cert_id,
            "cert_fingerprint": cert_fingerprint,
            "artifact_path": artifact_path or "",
            "artifact_sha256": artifact_sha256,
            "actor": actor,
            "hsm_vendor": hsm_vendor,
            "reason_code": reason_code,
            "ts": round(ts_v, 6),
            "extra": dict(extra or {}),
        }
        prev = self.entries[-1]["curr_hash"] if self.entries else ""
        record["prev_hash"] = prev
        record["curr_hash"] = _sign_chain_hash(prev, record)
        self.entries.append(record)
        if self.persist:
            self._fire_audit(record)
        return record

    def verify(self) -> tuple[bool, int | None]:
        prev = ""
        for i, rec in enumerate(self.entries):
            saved_curr = rec.get("curr_hash")
            saved_prev = rec.get("prev_hash")
            payload = {k: v for k, v in rec.items() if k != "curr_hash"}
            payload["prev_hash"] = prev
            recomputed = _sign_chain_hash(prev, payload)
            if saved_prev != prev or saved_curr != recomputed:
                return (False, i)
            prev = saved_curr
        return (True, None)

    def head(self) -> str:
        return self.entries[-1]["curr_hash"] if self.entries else ""

    def for_cert(self, cert_id: str) -> list[dict[str, Any]]:
        return [r for r in self.entries if r["cert_id"] == cert_id]

    def for_artifact(self, artifact_sha256: str) -> list[dict[str, Any]]:
        return [
            r for r in self.entries if r["artifact_sha256"] == artifact_sha256
        ]

    @staticmethod
    def _fire_audit(record: dict[str, Any]) -> None:
        try:
            from backend import audit
            audit.log_sync(
                action=f"codesign.{record['reason_code']}",
                entity_kind=CODESIGN_AUDIT_ENTITY_KIND,
                entity_id=record["cert_id"],
                after=record,
                actor=record.get("actor", "system"),
            )
        except Exception as exc:  # pragma: no cover
            logger.debug("codesign audit fire-and-forget failed: %s", exc)


_global_audit_chain: CodeSignAuditChain | None = None


def get_global_audit_chain() -> CodeSignAuditChain:
    global _global_audit_chain
    if _global_audit_chain is None:
        _global_audit_chain = CodeSignAuditChain()
    return _global_audit_chain


def reset_global_audit_chain_for_tests() -> None:
    global _global_audit_chain
    _global_audit_chain = None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  5. Sign-attestation facade
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass(frozen=True)
class CodeSignContext:
    """Opaque handle the signing transport receives.

    Carries the cert record (for Team ID / alias / HSM ref), the
    artifact fingerprint, the actor, and the audit entry that the
    sign call produced.  Never carries plaintext key material; if the
    caller needs it, it calls :meth:`CodesignStore.decrypt_material`
    explicitly and accepts the responsibility.
    """

    cert: CertRecord
    artifact_path: str
    artifact_sha256: str
    actor: str
    audit_entry: dict[str, Any]
    hsm_provider: HSMProvider | None


def attest_sign(
    *,
    cert_id: str,
    artifact_path: str,
    artifact_sha256: str,
    actor: str,
    reason_code: str = "sign",
    store: CodesignStore | None = None,
    chain: CodeSignAuditChain | None = None,
    ts: float | None = None,
    extra: dict[str, Any] | None = None,
) -> CodeSignContext:
    """Record a sign attempt in the hash-chain and return a context.

    P3 does not execute the actual ``codesign`` / ``apksigner`` call —
    that belongs in the mobile build transport (P5 #290 store upload,
    P2 #287 simulate track).  This function is the **attestation
    hook**: every sign the pipeline performs flows through here so
    the audit log records who / when / what artifact / what cert.
    """
    s = store or get_store()
    c = chain or get_global_audit_chain()
    record = s.get(cert_id)
    if record.is_expired(now=ts):
        raise SigningChainError(
            f"refusing to sign with expired cert {cert_id} "
            f"(expired {abs(record.days_until_expiry(now=ts)):.1f} days ago)",
        )
    hsm_provider = (
        HSMProvider(vendor=record.hsm_vendor, key_ref=record.hsm_key_ref)
        if record.hsm_vendor is not HSMVendor.none
        else None
    )
    entry = c.append(
        cert_id=cert_id,
        cert_fingerprint=record.fingerprint_sha256,
        artifact_path=artifact_path,
        artifact_sha256=artifact_sha256,
        actor=actor,
        hsm_vendor=record.hsm_vendor.value,
        reason_code=reason_code,
        ts=ts,
        extra=extra,
    )
    return CodeSignContext(
        cert=record,
        artifact_path=artifact_path,
        artifact_sha256=artifact_sha256,
        actor=actor,
        audit_entry=entry,
        hsm_provider=hsm_provider,
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  6. Expiry scanning + SSE alerts
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass(frozen=True)
class CertExpiryFinding:
    cert_id: str
    cert_kind: str
    days_left: float
    threshold_days: int
    severity: str
    not_after: float


def severity_for_days(days_left: float) -> str | None:
    """Return the SSE severity bucket for a remaining-days value.

    - ``days_left <= 1``  → ``critical``
    - ``days_left <= 7``  → ``warn``
    - ``days_left <= 30`` → ``notice``
    - otherwise           → ``None`` (no alert)

    Negative / zero inputs bucket into ``critical`` (already expired).
    """
    for threshold in EXPIRY_THRESHOLD_DAYS:
        if days_left <= threshold:
            return _SEVERITY_BY_THRESHOLD[threshold]
    return None


def check_cert_expiries(
    *,
    now: float | None = None,
    store: CodesignStore | None = None,
) -> list[CertExpiryFinding]:
    """Walk the store and return findings for every cert within 30d of expiry.

    Pure — does not emit SSE.  Callers that want the alert side-effect
    call :func:`fire_expiry_alerts` which delegates to this function
    and pushes one event per finding.
    """
    s = store or get_store()
    now_v = float(now) if now is not None else time.time()
    out: list[CertExpiryFinding] = []
    for rec in s.list_records():
        days_left = rec.days_until_expiry(now=now_v)
        if days_left > EXPIRY_THRESHOLD_DAYS[-1]:
            continue
        threshold = next(
            t for t in EXPIRY_THRESHOLD_DAYS if days_left <= t
        )
        out.append(
            CertExpiryFinding(
                cert_id=rec.cert_id,
                cert_kind=rec.kind.value,
                days_left=round(days_left, 3),
                threshold_days=threshold,
                severity=_SEVERITY_BY_THRESHOLD[threshold],
                not_after=rec.not_after,
            ),
        )
    return out


def fire_expiry_alerts(
    *,
    now: float | None = None,
    store: CodesignStore | None = None,
    publisher: Any = None,
) -> list[CertExpiryFinding]:
    """Emit one ``cert_expiry`` SSE event per finding and return them.

    ``publisher`` may be injected by tests to capture events without
    importing :mod:`backend.events`; default resolves at call-time so
    the module stays import-light.
    """
    findings = check_cert_expiries(now=now, store=store)
    pub = publisher or _default_publisher()
    for f in findings:
        pub(
            cert_id=f.cert_id,
            cert_kind=f.cert_kind,
            days_left=f.days_left,
            threshold_days=f.threshold_days,
            severity=f.severity,
            not_after=f.not_after,
        )
    return findings


def _default_publisher():
    try:
        from backend.events import bus, _log

        def _publish(**payload: Any) -> None:
            severity = payload["severity"]
            message = (
                f"cert {payload['cert_id']} ({payload['cert_kind']}) "
                f"expires in {payload['days_left']:.2f} days "
                f"(<= {payload['threshold_days']}d threshold)"
            )
            bus.publish("cert_expiry", payload, broadcast_scope="global")
            level = {
                "notice": "warn",
                "warn": "warn",
                "critical": "error",
            }.get(severity, "warn")
            _log(f"[CODESIGN] {severity.upper()}: {message}", level)

        return _publish
    except Exception:  # pragma: no cover — fallback for import-isolated tests
        def _noop(**_: Any) -> None:
            pass
        return _noop
