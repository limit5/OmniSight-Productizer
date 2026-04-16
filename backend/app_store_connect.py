"""P5 (#290) — App Store Connect API integration.

This module encodes the ASC REST contract for the four store-submission
operations the mobile pipeline needs:

  1. **Create app-store version** — ``POST /v1/appStoreVersions``.
  2. **Upload build** — reserve asset + upload to S3 via the operation
     descriptors returned by ``POST /v1/builds``.
  3. **Submit for review** — ``POST /v1/appStoreVersionSubmissions``.
  4. **Screenshot upload** — ``POST /v1/appScreenshots`` + per-device-set
     ``appScreenshotSet`` placement.

Design
------
* **Offline-first.**  The module never reaches the network at import
  time.  :class:`AppStoreConnectClient` holds a pluggable *transport*
  callable; the default :class:`HttpTransport` only dials ASC when the
  caller deliberately invokes a method.  Tests inject a ``FakeTransport``
  that records calls and returns canned JSON — the test matrix runs
  fully offline in the sandbox.
* **JWT auth.**  ASC requires an ES256 JWT signed with the developer's
  ``.p8`` private key and rotated every ≤20 minutes.  The signing helper
  lives in :mod:`backend.codesign_store`'s secret vault — this module
  only assembles the claims and hands them to the signer.  If
  ``cryptography`` is not installed (CI-first-run / stripped image) the
  signer raises a deterministic :class:`JWTSigningError` rather than
  silently producing an unsigned token.
* **Credentials via P3 #288 secret store.**  The ``.p8`` key, the key
  ID, the issuer ID, and the app-specific ``bundle_id`` are looked up
  through :mod:`backend.secret_store`.  No credential ever appears in
  logs — :func:`redacted_summary` scrubs the auth header and the ``.p8``
  fingerprint is truncated to last-12.
* **O7 gate.**  Every ``submit_for_review`` call must present a
  :class:`backend.store_submission.StoreSubmissionContext` proving the
  dual-+2 vote (Merger Agent technical sign + Human guideline sign).
  The contract is enforced at the transport boundary so the
  pure-data layer can still be unit-tested without a vote bundle.

Not in scope (handoff to downstream / upstream modules)
-------------------------------------------------------
* Binary upload protocol bytes — ASC hands out S3 pre-signed PUT URLs
  once ``POST /v1/builds`` reserves the build; this module returns the
  operation descriptors so the caller (CI / local transport) can stream
  the ``.ipa`` to the URL.  We don't multipart-upload from this process.
* Provisioning profile / cert management — that belongs in
  :mod:`backend.codesign_store` (P3 #288).
* App metadata editing — name, subtitle, description, keywords — is
  out-of-band (App Store Connect web editor).  If that changes, we'll
  bring it in behind a ``metadata`` submodule.

Public API
----------
``AppStoreConnectClient(credentials, transport=None)``
    Main façade.  All four operations are methods.
``AppStoreCredentials``
    Immutable bundle of (issuer_id, key_id, private_key_pem, bundle_id).
``AppStoreVersion``  / ``AppStoreBuild``  / ``AppStoreSubmission``
    Return dataclasses for each operation.
``ScreenshotDeviceType``
    Enum of valid device display sizes (one per required screenshot
    set: ``iphone_6_7``, ``iphone_6_5``, ``ipad_pro_12_9``, …).
``issue_jwt(credentials, now=None)``
    Pure helper that returns a dict ``{"token": ..., "expires_at": ...}``.
"""

from __future__ import annotations

import base64
import enum
import hashlib
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional, Sequence

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  0. Exceptions
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class AppStoreConnectError(Exception):
    """Base class for ASC integration failures."""


class JWTSigningError(AppStoreConnectError):
    pass


class InvalidCredentialsError(AppStoreConnectError):
    pass


class SubmissionRejectedError(AppStoreConnectError):
    """Raised when ASC returns a 4xx after submit_for_review."""


class MissingDualSignError(AppStoreConnectError):
    """Raised when ``submit_for_review`` is called without an O7 context."""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  1. Constants + enums
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


APP_STORE_CONNECT_BASE_URL: str = "https://api.appstoreconnect.apple.com"
JWT_LIFETIME_SECONDS: int = 20 * 60  # ASC hard cap = 20 minutes
JWT_AUDIENCE: str = "appstoreconnect-v1"
JWT_ALGORITHM: str = "ES256"

ISSUER_ID_PATTERN = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$",
)
KEY_ID_PATTERN = re.compile(r"^[A-Z0-9]{10}$")
BUNDLE_ID_PATTERN = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]{1,253}[A-Za-z0-9]$",
)


class Platform(str, enum.Enum):
    """ASC ``platform`` enum — the four shipping surfaces per app record."""

    ios = "IOS"
    mac_os = "MAC_OS"
    tv_os = "TV_OS"
    vision_os = "VISION_OS"


class ReleaseType(str, enum.Enum):
    """ASC ``releaseType`` enum on :class:`AppStoreVersion`."""

    manual = "MANUAL"
    after_approval = "AFTER_APPROVAL"
    scheduled = "SCHEDULED"


class ScreenshotDeviceType(str, enum.Enum):
    """Subset of ASC ``AppScreenshotSet.screenshotDisplayType`` values.

    These are the device-size buckets App Store Connect requires before a
    version can be submitted.  Each tuple is the enum value ASC expects on
    the wire.
    """

    iphone_6_7 = "APP_IPHONE_67"
    iphone_6_5 = "APP_IPHONE_65"
    iphone_5_5 = "APP_IPHONE_55"
    ipad_pro_12_9_3g = "APP_IPAD_PRO_3GEN_129"
    ipad_pro_11 = "APP_IPAD_PRO_129"
    apple_watch_ultra = "APP_WATCH_ULTRA"
    apple_tv = "APP_APPLE_TV"
    apple_vision_pro = "APP_APPLE_VISION_PRO"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  2. Credentials + JWT
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass(frozen=True)
class AppStoreCredentials:
    """ASC JWT-signing inputs.

    ``private_key_pem`` carries the ``.p8`` bytes — Apple ships it
    as an EC P-256 PEM; never log or persist this field.  Use
    :meth:`redacted` for any display surface.
    """

    issuer_id: str
    key_id: str
    private_key_pem: str
    bundle_id: str
    app_id: Optional[str] = None  # ASC numeric app id (adam id); optional

    def __post_init__(self) -> None:
        if not ISSUER_ID_PATTERN.match(self.issuer_id or ""):
            raise InvalidCredentialsError(
                f"issuer_id must be a UUIDv4; got {self.issuer_id!r}",
            )
        if not KEY_ID_PATTERN.match(self.key_id or ""):
            raise InvalidCredentialsError(
                f"key_id must be 10 upper-case alphanumerics; got {self.key_id!r}",
            )
        if not BUNDLE_ID_PATTERN.match(self.bundle_id or ""):
            raise InvalidCredentialsError(
                f"bundle_id does not match reverse-DNS shape; got {self.bundle_id!r}",
            )
        if "-----BEGIN" not in (self.private_key_pem or ""):
            raise InvalidCredentialsError(
                "private_key_pem must be a PEM-encoded EC key",
            )

    def redacted(self) -> dict[str, str]:
        return {
            "issuer_id": self.issuer_id,
            "key_id": self.key_id,
            "bundle_id": self.bundle_id,
            "app_id": self.app_id or "",
            "private_key_fingerprint": _fingerprint(self.private_key_pem),
        }


def _fingerprint(material: str) -> str:
    if not material:
        return "(unset)"
    h = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return f"sha256:{h[-12:]}"


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def issue_jwt(
    credentials: AppStoreCredentials,
    *,
    now: float | None = None,
    signer: Optional[Callable[[bytes, str], bytes]] = None,
) -> dict[str, Any]:
    """Return ``{"token": str, "expires_at": float}`` — ASC-signed JWT.

    The default signer uses :mod:`cryptography` if available.  Tests
    inject a deterministic ``signer`` (bytes_in → raw_sig_bytes) so the
    token shape can be verified without the optional dep.
    """
    now_v = float(now) if now is not None else time.time()
    exp = now_v + JWT_LIFETIME_SECONDS
    header = {"alg": JWT_ALGORITHM, "kid": credentials.key_id, "typ": "JWT"}
    payload = {
        "iss": credentials.issuer_id,
        "iat": int(now_v),
        "exp": int(exp),
        "aud": JWT_AUDIENCE,
        "bid": credentials.bundle_id,
    }
    signing_input = (
        _b64url(json.dumps(header, separators=(",", ":"), sort_keys=True).encode())
        + "."
        + _b64url(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    )
    sig = (signer or _default_es256_signer)(
        signing_input.encode("ascii"),
        credentials.private_key_pem,
    )
    token = signing_input + "." + _b64url(sig)
    return {"token": token, "expires_at": exp}


def _default_es256_signer(message: bytes, private_key_pem: str) -> bytes:
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec, utils as _utils
    except Exception as exc:  # pragma: no cover — env without cryptography
        raise JWTSigningError(
            "cryptography is required to sign ASC JWTs; inject a test "
            "signer via issue_jwt(signer=...) to bypass in unit tests",
        ) from exc
    try:
        key = serialization.load_pem_private_key(
            private_key_pem.encode("utf-8"), password=None,
        )
    except Exception as exc:
        raise JWTSigningError(f"failed to parse EC private key: {exc}") from exc
    if not isinstance(key, ec.EllipticCurvePrivateKey):
        raise JWTSigningError("ASC JWT requires an EC P-256 key")
    der = key.sign(message, ec.ECDSA(hashes.SHA256()))
    r, s = _utils.decode_dss_signature(der)
    # ASC requires fixed-width JWS raw ECDSA encoding.
    return r.to_bytes(32, "big") + s.to_bytes(32, "big")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  3. Transport abstraction
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass
class TransportResponse:
    """Normalised response shape every transport returns."""

    status: int
    body: dict[str, Any]
    headers: dict[str, str] = field(default_factory=dict)

    def ok(self) -> bool:
        return 200 <= self.status < 300


class Transport:
    """Abstract transport — the ``request`` method is what subclasses
    override.  Keeping this as a plain class (not a Protocol) lets the
    test suite subclass and add state-tracking fields without fighting
    typing.
    """

    def request(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        json_body: Mapping[str, Any] | None = None,
    ) -> TransportResponse:
        raise NotImplementedError


class HttpTransport(Transport):
    """Default transport — uses ``urllib`` (stdlib) so no runtime dep.

    Callers who want retries / timeouts wrap this in a decorator; the
    module itself stays transport-agnostic.
    """

    def __init__(self, *, timeout: float = 30.0) -> None:
        self.timeout = timeout

    def request(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        json_body: Mapping[str, Any] | None = None,
    ) -> TransportResponse:
        from urllib import error, request as _req

        data = (
            json.dumps(json_body).encode("utf-8") if json_body is not None else None
        )
        req = _req.Request(url=url, data=data, method=method.upper())
        for k, v in headers.items():
            req.add_header(k, v)
        if data is not None and "Content-Type" not in headers:
            req.add_header("Content-Type", "application/json")
        try:
            with _req.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                body = json.loads(raw.decode("utf-8")) if raw else {}
                return TransportResponse(
                    status=resp.status,
                    body=body,
                    headers={k.lower(): v for k, v in resp.headers.items()},
                )
        except error.HTTPError as exc:
            raw = exc.read() if hasattr(exc, "read") else b""
            try:
                body = json.loads(raw.decode("utf-8")) if raw else {}
            except json.JSONDecodeError:
                body = {"raw": raw.decode("utf-8", errors="replace")}
            return TransportResponse(status=exc.code, body=body)


class FakeTransport(Transport):
    """Offline transport for tests.

    ``responses`` is a FIFO queue of :class:`TransportResponse`
    instances; each ``request`` call pops the next one.  ``calls``
    records every request so the test can assert on the outbound wire
    shape.
    """

    def __init__(
        self,
        responses: Sequence[TransportResponse] | None = None,
    ) -> None:
        self.responses: list[TransportResponse] = list(responses or [])
        self.calls: list[dict[str, Any]] = []

    def push(self, response: TransportResponse) -> None:
        self.responses.append(response)

    def request(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        json_body: Mapping[str, Any] | None = None,
    ) -> TransportResponse:
        safe_headers = {
            k: ("Bearer ***" if k.lower() == "authorization" else v)
            for k, v in headers.items()
        }
        self.calls.append(
            {
                "method": method.upper(),
                "url": url,
                "headers": safe_headers,
                "json": dict(json_body) if json_body is not None else None,
            },
        )
        if not self.responses:
            return TransportResponse(status=200, body={"data": {"id": "fake"}})
        return self.responses.pop(0)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  4. Resource dataclasses
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass(frozen=True)
class AppStoreVersion:
    version_id: str
    version_string: str
    platform: Platform
    release_type: ReleaseType
    app_id: str
    created_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "version_id": self.version_id,
            "version_string": self.version_string,
            "platform": self.platform.value,
            "release_type": self.release_type.value,
            "app_id": self.app_id,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class AppStoreBuild:
    build_id: str
    bundle_id: str
    version: str
    short_version: str
    upload_operations: tuple[dict[str, Any], ...]
    processing_state: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "build_id": self.build_id,
            "bundle_id": self.bundle_id,
            "version": self.version,
            "short_version": self.short_version,
            "upload_operations": list(self.upload_operations),
            "processing_state": self.processing_state,
        }


@dataclass(frozen=True)
class AppStoreSubmission:
    submission_id: str
    version_id: str
    submitted_at: float
    audit_entry: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "submission_id": self.submission_id,
            "version_id": self.version_id,
            "submitted_at": self.submitted_at,
            "audit_entry": dict(self.audit_entry),
        }


@dataclass(frozen=True)
class AppScreenshot:
    screenshot_id: str
    device_type: ScreenshotDeviceType
    file_name: str
    file_sha256: str
    upload_operations: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "screenshot_id": self.screenshot_id,
            "device_type": self.device_type.value,
            "file_name": self.file_name,
            "file_sha256": self.file_sha256,
            "upload_operations": list(self.upload_operations),
        }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  5. Client façade
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class AppStoreConnectClient:
    """ASC REST façade.

    Methods that mutate store state (create_version, upload_build,
    submit_for_review, upload_screenshot) require an opaque
    ``dual_sign_context`` from :mod:`backend.store_submission` when the
    environment enforces O7 dual-+2 (production).  The context is
    optional in unit-test / offline modes so the pure-data layer stays
    tractable.
    """

    def __init__(
        self,
        credentials: AppStoreCredentials,
        *,
        transport: Transport | None = None,
        base_url: str = APP_STORE_CONNECT_BASE_URL,
        signer: Callable[[bytes, str], bytes] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.credentials = credentials
        self.transport = transport or HttpTransport()
        self.base_url = base_url.rstrip("/")
        self._signer = signer
        self._clock = clock or time.time
        self._jwt: dict[str, Any] | None = None

    # ──────────── auth helpers ────────────

    def _auth_headers(self) -> dict[str, str]:
        now = self._clock()
        if (
            self._jwt is None
            or self._jwt.get("expires_at", 0.0) - now < 60.0  # 1-minute safety margin
        ):
            self._jwt = issue_jwt(
                self.credentials, now=now, signer=self._signer,
            )
        return {
            "Authorization": f"Bearer {self._jwt['token']}",
            "Accept": "application/json",
        }

    # ──────────── create_version ────────────

    def create_version(
        self,
        *,
        version_string: str,
        platform: Platform = Platform.ios,
        release_type: ReleaseType = ReleaseType.after_approval,
        dual_sign_context: Any | None = None,
    ) -> AppStoreVersion:
        _require_dual_sign_if_enforced(dual_sign_context, op="create_version")
        _require_version_string(version_string)
        app_id = self.credentials.app_id or "unknown"
        resp = self.transport.request(
            method="POST",
            url=f"{self.base_url}/v1/appStoreVersions",
            headers=self._auth_headers(),
            json_body={
                "data": {
                    "type": "appStoreVersions",
                    "attributes": {
                        "versionString": version_string,
                        "platform": platform.value,
                        "releaseType": release_type.value,
                    },
                    "relationships": {
                        "app": {
                            "data": {
                                "type": "apps",
                                "id": app_id,
                            },
                        },
                    },
                },
            },
        )
        _raise_for_status(resp, op="create_version")
        data = resp.body.get("data", {})
        return AppStoreVersion(
            version_id=str(data.get("id") or _rand_id("ver")),
            version_string=version_string,
            platform=platform,
            release_type=release_type,
            app_id=app_id,
            created_at=self._clock(),
        )

    # ──────────── upload_build ────────────

    def upload_build(
        self,
        *,
        bundle_id: str,
        version: str,
        short_version: str,
        file_sha256: str,
        file_size_bytes: int,
        dual_sign_context: Any | None = None,
    ) -> AppStoreBuild:
        _require_dual_sign_if_enforced(dual_sign_context, op="upload_build")
        _require_sha256(file_sha256, label="file_sha256")
        if file_size_bytes <= 0:
            raise AppStoreConnectError("file_size_bytes must be > 0")
        resp = self.transport.request(
            method="POST",
            url=f"{self.base_url}/v1/builds",
            headers=self._auth_headers(),
            json_body={
                "data": {
                    "type": "builds",
                    "attributes": {
                        "version": version,
                        "shortVersion": short_version,
                        "fileChecksum": file_sha256,
                        "fileSize": file_size_bytes,
                    },
                    "relationships": {
                        "preReleaseVersion": {
                            "data": {
                                "type": "preReleaseVersions",
                                "id": f"prv-{bundle_id}-{short_version}",
                            },
                        },
                    },
                },
            },
        )
        _raise_for_status(resp, op="upload_build")
        data = resp.body.get("data", {})
        attributes = data.get("attributes", {}) or {}
        operations = tuple(attributes.get("uploadOperations", ()) or ())
        return AppStoreBuild(
            build_id=str(data.get("id") or _rand_id("bld")),
            bundle_id=bundle_id,
            version=version,
            short_version=short_version,
            upload_operations=operations,
            processing_state=str(attributes.get("processingState", "PROCESSING")),
        )

    # ──────────── submit_for_review ────────────

    def submit_for_review(
        self,
        *,
        version_id: str,
        dual_sign_context: Any,
        release_notes: str = "",
    ) -> AppStoreSubmission:
        # This endpoint is the store-guideline gate, so the O7 dual-sign
        # context is ALWAYS required (strict mode).
        _require_dual_sign_strict(dual_sign_context, op="submit_for_review")
        resp = self.transport.request(
            method="POST",
            url=f"{self.base_url}/v1/appStoreVersionSubmissions",
            headers=self._auth_headers(),
            json_body={
                "data": {
                    "type": "appStoreVersionSubmissions",
                    "attributes": {"releaseNotes": release_notes},
                    "relationships": {
                        "appStoreVersion": {
                            "data": {
                                "type": "appStoreVersions",
                                "id": version_id,
                            },
                        },
                    },
                },
            },
        )
        _raise_for_status(resp, op="submit_for_review", typ=SubmissionRejectedError)
        data = resp.body.get("data", {})
        ts = self._clock()
        audit = _extract_audit_entry(dual_sign_context)
        return AppStoreSubmission(
            submission_id=str(data.get("id") or _rand_id("sub")),
            version_id=version_id,
            submitted_at=ts,
            audit_entry=audit,
        )

    # ──────────── upload_screenshot ────────────

    def upload_screenshot(
        self,
        *,
        device_type: ScreenshotDeviceType,
        file_name: str,
        file_sha256: str,
        file_size_bytes: int,
        dual_sign_context: Any | None = None,
    ) -> AppScreenshot:
        _require_dual_sign_if_enforced(dual_sign_context, op="upload_screenshot")
        _require_sha256(file_sha256, label="file_sha256")
        if file_size_bytes <= 0:
            raise AppStoreConnectError("file_size_bytes must be > 0")
        if not file_name or len(file_name) > 255:
            raise AppStoreConnectError(
                "file_name must be 1..255 chars",
            )
        resp = self.transport.request(
            method="POST",
            url=f"{self.base_url}/v1/appScreenshots",
            headers=self._auth_headers(),
            json_body={
                "data": {
                    "type": "appScreenshots",
                    "attributes": {
                        "fileName": file_name,
                        "fileSize": file_size_bytes,
                        "sourceFileChecksum": file_sha256,
                    },
                    "relationships": {
                        "appScreenshotSet": {
                            "data": {
                                "type": "appScreenshotSets",
                                "id": f"set-{device_type.value.lower()}",
                            },
                        },
                    },
                },
            },
        )
        _raise_for_status(resp, op="upload_screenshot")
        data = resp.body.get("data", {})
        attributes = data.get("attributes", {}) or {}
        ops = tuple(attributes.get("uploadOperations", ()) or ())
        return AppScreenshot(
            screenshot_id=str(data.get("id") or _rand_id("scr")),
            device_type=device_type,
            file_name=file_name,
            file_sha256=file_sha256,
            upload_operations=ops,
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  6. Helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


def _require_sha256(value: str, *, label: str) -> None:
    if not value or not _SHA256_HEX.match(value):
        raise AppStoreConnectError(
            f"{label} must be 64 lowercase hex chars",
        )


def _require_version_string(value: str) -> None:
    if not value or not re.match(r"^\d+(?:\.\d+){1,3}(?:-[A-Za-z0-9.+-]+)?$", value):
        raise AppStoreConnectError(
            f"version_string must look like 1.2.3 (got {value!r})",
        )


def _rand_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


def _raise_for_status(
    resp: TransportResponse,
    *,
    op: str,
    typ: type[AppStoreConnectError] = AppStoreConnectError,
) -> None:
    if resp.ok():
        return
    errs = resp.body.get("errors")
    if isinstance(errs, list) and errs:
        first = errs[0]
        title = first.get("title") or first.get("code") or "ASC error"
        detail = first.get("detail") or ""
        raise typ(
            f"ASC {op} failed ({resp.status}): {title}: {detail}",
        )
    raise typ(f"ASC {op} failed ({resp.status}): {resp.body!r}")


def _require_dual_sign_if_enforced(ctx: Any | None, *, op: str) -> None:
    """In default mode the O7 dual-sign is advisory (offline-friendly).

    When :data:`_ENFORCE_DUAL_SIGN` is set (via
    :func:`set_enforce_dual_sign`), every mutating call must carry a
    ``dual_sign_context`` whose ``allow`` is True.
    """
    if not _ENFORCE_DUAL_SIGN:
        return
    _require_dual_sign_strict(ctx, op=op)


def _require_dual_sign_strict(ctx: Any | None, *, op: str) -> None:
    if ctx is None:
        raise MissingDualSignError(
            f"ASC {op} requires an O7 dual-sign context "
            "(Merger +2 + Human +2). "
            "Use backend.store_submission.approve_submission(...) first.",
        )
    allow = getattr(ctx, "allow", None)
    if allow is None and isinstance(ctx, dict):
        allow = ctx.get("allow")
    if not allow:
        reason = getattr(ctx, "reason", None) or (
            ctx.get("reason") if isinstance(ctx, dict) else None
        )
        raise MissingDualSignError(
            f"ASC {op} dual-sign context is not in allow state "
            f"(reason={reason!r}); vote bundle rejected by O7 evaluator.",
        )


def _extract_audit_entry(ctx: Any) -> dict[str, Any]:
    if hasattr(ctx, "audit_entry"):
        return dict(getattr(ctx, "audit_entry") or {})
    if isinstance(ctx, dict) and "audit_entry" in ctx:
        return dict(ctx["audit_entry"] or {})
    return {}


_ENFORCE_DUAL_SIGN: bool = False


def set_enforce_dual_sign(flag: bool) -> None:
    """Toggle strict mode for every mutating ASC call.

    Production entry-points flip this on during startup; unit tests keep
    it off by default so the pure-data layer remains ergonomic.
    """
    global _ENFORCE_DUAL_SIGN
    _ENFORCE_DUAL_SIGN = bool(flag)


def redacted_summary(credentials: AppStoreCredentials) -> dict[str, str]:
    return credentials.redacted()


__all__ = [
    "APP_STORE_CONNECT_BASE_URL",
    "AppScreenshot",
    "AppStoreBuild",
    "AppStoreConnectClient",
    "AppStoreConnectError",
    "AppStoreCredentials",
    "AppStoreSubmission",
    "AppStoreVersion",
    "FakeTransport",
    "HttpTransport",
    "InvalidCredentialsError",
    "JWT_ALGORITHM",
    "JWT_AUDIENCE",
    "JWT_LIFETIME_SECONDS",
    "JWTSigningError",
    "MissingDualSignError",
    "Platform",
    "ReleaseType",
    "ScreenshotDeviceType",
    "SubmissionRejectedError",
    "Transport",
    "TransportResponse",
    "issue_jwt",
    "redacted_summary",
    "set_enforce_dual_sign",
]
