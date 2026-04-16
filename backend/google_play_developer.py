"""P5 (#290) — Google Play Developer API integration.

The mobile pipeline talks to Play Developer v3 for two deliverables:

  1. **Upload the signed ``.aab``** — ``POST /edits/{editId}/bundles``.
  2. **Manage tracks** — ``PATCH /edits/{editId}/tracks/{track}`` for
     ``internal`` / ``alpha`` / ``beta`` / ``production`` staged rollouts.

Design
------
* **Offline-first transport.**  The module uses the same
  :class:`backend.app_store_connect.Transport` abstraction so
  ``FakeTransport`` can drive both store clients from a single shared
  fixture.  ``HttpTransport`` is the stdlib-only default.
* **Service-account JWT auth.**  Play uses RS256 over a JSON service
  account with the ``androidpublisher`` scope.  Same
  ``signer``-injection pattern as ASC — tests provide a deterministic
  signer and never need the network.
* **Edits == transactions.**  Play's publishing model requires wrapping
  every change (build upload + track update) in an *edit* session.
  :class:`GooglePlayEdit` is a context manager that opens, validates,
  commits, or aborts an edit.  The module exposes the helpers so the
  caller can decide whether to auto-commit or stage multiple changes.
* **Staged rollouts.**  :meth:`GooglePlayClient.update_track`
  accepts ``user_fraction`` (0.0..1.0).  The Play API expects the track
  ``status`` (``completed``, ``inProgress``, ``halted``, ``draft``) to
  match the fraction; the module enforces the invariant so the caller
  never ships a 100 % inProgress rollout by accident.
* **O7 gate.**  ``submit_to_production`` requires a
  :class:`backend.store_submission.StoreSubmissionContext` with
  ``allow=True`` — same contract as
  :meth:`AppStoreConnectClient.submit_for_review`.

Public API
----------
``GooglePlayClient(credentials, transport=None)``
``GooglePlayCredentials``
``GooglePlayEdit``  (context manager)
``Track`` / ``TrackStatus``
``UploadedBundle``  / ``TrackUpdate``
``issue_service_account_jwt(credentials)``
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

from backend.app_store_connect import (
    FakeTransport,
    HttpTransport,
    Transport,
    TransportResponse,
)

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  0. Exceptions
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class GooglePlayError(Exception):
    """Base class for Play Developer failures."""


class PlayAuthError(GooglePlayError):
    pass


class PlayInvalidCredentialsError(GooglePlayError):
    pass


class PlayEditError(GooglePlayError):
    pass


class PlayRolloutError(GooglePlayError):
    pass


class PlayMissingDualSignError(GooglePlayError):
    pass


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  1. Constants + enums
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


PLAY_DEVELOPER_BASE_URL: str = "https://androidpublisher.googleapis.com"
PLAY_TOKEN_URL: str = "https://oauth2.googleapis.com/token"
PLAY_SCOPE: str = "https://www.googleapis.com/auth/androidpublisher"
PLAY_TOKEN_LIFETIME: int = 3600
PLAY_JWT_ALGORITHM: str = "RS256"

PACKAGE_NAME_PATTERN = re.compile(
    r"^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+$",
)


class Track(str, enum.Enum):
    """Play publishing tracks."""

    internal = "internal"
    alpha = "alpha"
    beta = "beta"
    production = "production"


class TrackStatus(str, enum.Enum):
    """Play track release ``status`` enum."""

    completed = "completed"
    in_progress = "inProgress"
    halted = "halted"
    draft = "draft"


# ``production`` is the only track where staged rollouts are common.
STAGED_ROLLOUT_TRACKS: frozenset[Track] = frozenset({
    Track.production,
    Track.beta,
})


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  2. Credentials + JWT
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass(frozen=True)
class GooglePlayCredentials:
    """Play service-account inputs.

    Reads the Google service-account JSON field-by-field.  ``client_email``,
    ``private_key`` (RS256 PEM), and ``token_uri`` are enough to issue
    an access token; ``package_name`` selects the target app.
    """

    client_email: str
    private_key_pem: str
    package_name: str
    token_uri: str = PLAY_TOKEN_URL
    project_id: str = ""

    def __post_init__(self) -> None:
        if "@" not in (self.client_email or ""):
            raise PlayInvalidCredentialsError(
                f"client_email must be an email; got {self.client_email!r}",
            )
        if "-----BEGIN" not in (self.private_key_pem or ""):
            raise PlayInvalidCredentialsError(
                "private_key_pem must be a PEM-encoded RSA key",
            )
        if not PACKAGE_NAME_PATTERN.match(self.package_name or ""):
            raise PlayInvalidCredentialsError(
                f"package_name does not match reverse-DNS shape; "
                f"got {self.package_name!r}",
            )

    def redacted(self) -> dict[str, str]:
        return {
            "client_email": self.client_email,
            "package_name": self.package_name,
            "project_id": self.project_id,
            "private_key_fingerprint": _fingerprint(self.private_key_pem),
            "token_uri": self.token_uri,
        }


def _fingerprint(material: str) -> str:
    if not material:
        return "(unset)"
    h = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return f"sha256:{h[-12:]}"


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def issue_service_account_jwt(
    credentials: GooglePlayCredentials,
    *,
    now: float | None = None,
    scope: str = PLAY_SCOPE,
    signer: Optional[Callable[[bytes, str], bytes]] = None,
) -> dict[str, Any]:
    """Build the ``assertion`` JWT for the Google OAuth2 token exchange.

    Returns ``{"assertion": str, "expires_at": float}``; the caller
    exchanges the assertion at ``token_uri`` for a bearer access token.
    """
    now_v = float(now) if now is not None else time.time()
    exp = now_v + PLAY_TOKEN_LIFETIME
    header = {"alg": PLAY_JWT_ALGORITHM, "typ": "JWT"}
    payload = {
        "iss": credentials.client_email,
        "scope": scope,
        "aud": credentials.token_uri,
        "iat": int(now_v),
        "exp": int(exp),
    }
    signing_input = (
        _b64url(json.dumps(header, separators=(",", ":"), sort_keys=True).encode())
        + "."
        + _b64url(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    )
    sig = (signer or _default_rs256_signer)(
        signing_input.encode("ascii"),
        credentials.private_key_pem,
    )
    assertion = signing_input + "." + _b64url(sig)
    return {"assertion": assertion, "expires_at": exp}


def _default_rs256_signer(message: bytes, private_key_pem: str) -> bytes:
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding, rsa
    except Exception as exc:  # pragma: no cover — env without cryptography
        raise PlayAuthError(
            "cryptography is required to sign Play service-account JWTs; "
            "inject a test signer via issue_service_account_jwt(signer=...) "
            "to bypass in unit tests",
        ) from exc
    try:
        key = serialization.load_pem_private_key(
            private_key_pem.encode("utf-8"), password=None,
        )
    except Exception as exc:
        raise PlayAuthError(
            f"failed to parse Play RSA private key: {exc}",
        ) from exc
    if not isinstance(key, rsa.RSAPrivateKey):
        raise PlayAuthError("Play JWT requires an RSA private key")
    return key.sign(message, padding.PKCS1v15(), hashes.SHA256())


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  3. Data shapes
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass(frozen=True)
class UploadedBundle:
    version_code: int
    sha256: str
    sha1: str
    package_name: str
    upload_time: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "version_code": self.version_code,
            "sha256": self.sha256,
            "sha1": self.sha1,
            "package_name": self.package_name,
            "upload_time": self.upload_time,
        }


@dataclass(frozen=True)
class TrackUpdate:
    track: Track
    status: TrackStatus
    user_fraction: float
    version_codes: tuple[int, ...]
    release_notes: dict[str, str]
    audit_entry: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "track": self.track.value,
            "status": self.status.value,
            "user_fraction": self.user_fraction,
            "version_codes": list(self.version_codes),
            "release_notes": dict(self.release_notes),
            "audit_entry": dict(self.audit_entry),
        }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  4. Edit session
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class GooglePlayEdit:
    """Context manager wrapping a Play Developer ``edit`` transaction.

    ``__enter__`` opens the edit (``POST /edits``); ``__exit__`` commits
    (on normal exit) or aborts (on exception).  Calling ``commit()`` or
    ``abort()`` explicitly is also supported — the context manager
    becomes a no-op on exit once finalised.
    """

    def __init__(self, client: "GooglePlayClient") -> None:
        self._client = client
        self.edit_id: str | None = None
        self._finalised = False

    def __enter__(self) -> "GooglePlayEdit":
        resp = self._client._request(
            method="POST",
            path=f"/androidpublisher/v3/applications/"
            f"{self._client.credentials.package_name}/edits",
            json_body={},
        )
        _raise_for_status(resp, op="edit.open", typ=PlayEditError)
        self.edit_id = str(resp.body.get("id") or _rand_id("edit"))
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._finalised or self.edit_id is None:
            return
        if exc_type is None:
            self.commit()
        else:
            self.abort()

    def commit(self) -> None:
        if self._finalised:
            return
        if self.edit_id is None:
            raise PlayEditError("commit called before edit open")
        resp = self._client._request(
            method="POST",
            path=f"/androidpublisher/v3/applications/"
            f"{self._client.credentials.package_name}/edits/"
            f"{self.edit_id}:commit",
            json_body={},
        )
        _raise_for_status(resp, op="edit.commit", typ=PlayEditError)
        self._finalised = True

    def abort(self) -> None:
        if self._finalised:
            return
        if self.edit_id is None:
            return
        try:
            self._client._request(
                method="DELETE",
                path=f"/androidpublisher/v3/applications/"
                f"{self._client.credentials.package_name}/edits/"
                f"{self.edit_id}",
            )
        finally:
            self._finalised = True


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  5. Client façade
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class GooglePlayClient:
    """Play Developer v3 façade."""

    def __init__(
        self,
        credentials: GooglePlayCredentials,
        *,
        transport: Transport | None = None,
        base_url: str = PLAY_DEVELOPER_BASE_URL,
        signer: Callable[[bytes, str], bytes] | None = None,
        clock: Callable[[], float] | None = None,
        token_exchange: Callable[[str, str], dict[str, Any]] | None = None,
    ) -> None:
        self.credentials = credentials
        self.transport = transport or HttpTransport()
        self.base_url = base_url.rstrip("/")
        self._signer = signer
        self._clock = clock or time.time
        self._access_token: dict[str, Any] | None = None
        self._token_exchange = token_exchange or self._default_token_exchange

    # ──────────── auth ────────────

    def _auth_headers(self) -> dict[str, str]:
        now = self._clock()
        if (
            self._access_token is None
            or self._access_token.get("expires_at", 0.0) - now < 60.0
        ):
            assertion_bundle = issue_service_account_jwt(
                self.credentials, now=now, signer=self._signer,
            )
            self._access_token = self._token_exchange(
                assertion_bundle["assertion"], self.credentials.token_uri,
            )
        return {
            "Authorization": f"Bearer {self._access_token['access_token']}",
            "Accept": "application/json",
        }

    def _default_token_exchange(
        self, assertion: str, token_uri: str,
    ) -> dict[str, Any]:
        """Default token exchange dials the real Google endpoint.

        Tests inject a ``token_exchange`` so nothing touches the
        network.  Production wraps this with retries at the caller.
        """
        from urllib import error, parse as _parse, request as _req

        form = _parse.urlencode({
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": assertion,
        }).encode()
        try:
            with _req.urlopen(
                _req.Request(
                    token_uri,
                    data=form,
                    method="POST",
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                ),
                timeout=30,
            ) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except error.HTTPError as exc:
            raise PlayAuthError(
                f"Play token exchange failed {exc.code}: "
                f"{exc.read().decode('utf-8', errors='replace')}",
            ) from exc
        expires_in = int(payload.get("expires_in") or PLAY_TOKEN_LIFETIME)
        return {
            "access_token": payload["access_token"],
            "expires_at": self._clock() + expires_in,
        }

    def _request(
        self,
        *,
        method: str,
        path: str,
        json_body: Mapping[str, Any] | None = None,
        extra_headers: Mapping[str, str] | None = None,
    ) -> TransportResponse:
        headers = dict(self._auth_headers())
        if extra_headers:
            headers.update(extra_headers)
        return self.transport.request(
            method=method,
            url=f"{self.base_url}{path}",
            headers=headers,
            json_body=json_body,
        )

    # ──────────── edit session ────────────

    def new_edit(self) -> GooglePlayEdit:
        return GooglePlayEdit(self)

    # ──────────── upload_bundle ────────────

    def upload_bundle(
        self,
        *,
        edit: GooglePlayEdit,
        aab_sha256: str,
        aab_sha1: str,
        version_code: int,
        dual_sign_context: Any | None = None,
    ) -> UploadedBundle:
        _require_edit(edit)
        _require_dual_sign_if_enforced(dual_sign_context, op="upload_bundle")
        _require_sha256(aab_sha256)
        _require_sha1(aab_sha1)
        if version_code < 1:
            raise GooglePlayError("version_code must be >= 1")
        resp = self._request(
            method="POST",
            path=(
                f"/androidpublisher/v3/applications/"
                f"{self.credentials.package_name}/edits/{edit.edit_id}/bundles"
            ),
            json_body={
                "sha256": aab_sha256,
                "sha1": aab_sha1,
                "versionCode": version_code,
            },
            extra_headers={"Content-Type": "application/octet-stream"},
        )
        _raise_for_status(resp, op="upload_bundle", typ=GooglePlayError)
        return UploadedBundle(
            version_code=int(resp.body.get("versionCode", version_code)),
            sha256=str(resp.body.get("sha256", aab_sha256)),
            sha1=str(resp.body.get("sha1", aab_sha1)),
            package_name=self.credentials.package_name,
            upload_time=self._clock(),
        )

    # ──────────── update_track ────────────

    def update_track(
        self,
        *,
        edit: GooglePlayEdit,
        track: Track,
        version_codes: Sequence[int],
        status: TrackStatus,
        user_fraction: float = 1.0,
        release_notes: Mapping[str, str] | None = None,
        dual_sign_context: Any | None = None,
    ) -> TrackUpdate:
        _require_edit(edit)
        _require_dual_sign_if_enforced(dual_sign_context, op="update_track")
        if track is Track.production and dual_sign_context is None:
            raise PlayMissingDualSignError(
                "production track update requires an O7 dual-sign context",
            )
        _require_dual_sign_strict_if_production(
            track=track, ctx=dual_sign_context,
        )
        codes = tuple(int(c) for c in version_codes)
        if not codes:
            raise GooglePlayError("version_codes must not be empty")
        _validate_rollout(track=track, status=status, user_fraction=user_fraction)
        payload: dict[str, Any] = {
            "track": track.value,
            "releases": [
                {
                    "status": status.value,
                    "versionCodes": [str(c) for c in codes],
                    "userFraction": user_fraction,
                    "releaseNotes": [
                        {"language": lang, "text": text}
                        for lang, text in (release_notes or {}).items()
                    ],
                },
            ],
        }
        resp = self._request(
            method="PATCH",
            path=(
                f"/androidpublisher/v3/applications/"
                f"{self.credentials.package_name}/edits/{edit.edit_id}/tracks/"
                f"{track.value}"
            ),
            json_body=payload,
        )
        _raise_for_status(resp, op="update_track", typ=GooglePlayError)
        return TrackUpdate(
            track=track,
            status=status,
            user_fraction=user_fraction,
            version_codes=codes,
            release_notes=dict(release_notes or {}),
            audit_entry=_extract_audit_entry(dual_sign_context),
        )

    # ──────────── convenience: submit_to_production ────────────

    def submit_to_production(
        self,
        *,
        version_code: int,
        dual_sign_context: Any,
        user_fraction: float = 0.1,
        release_notes: Mapping[str, str] | None = None,
    ) -> TrackUpdate:
        _require_dual_sign_strict(dual_sign_context, op="submit_to_production")
        status = (
            TrackStatus.in_progress if user_fraction < 1.0 else TrackStatus.completed
        )
        with self.new_edit() as edit:
            update = self.update_track(
                edit=edit,
                track=Track.production,
                version_codes=[version_code],
                status=status,
                user_fraction=user_fraction,
                release_notes=release_notes,
                dual_sign_context=dual_sign_context,
            )
        return update


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  6. Validation helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
_SHA1_HEX = re.compile(r"^[0-9a-f]{40}$")


def _require_sha256(value: str) -> None:
    if not value or not _SHA256_HEX.match(value):
        raise GooglePlayError("sha256 must be 64 lowercase hex chars")


def _require_sha1(value: str) -> None:
    if not value or not _SHA1_HEX.match(value):
        raise GooglePlayError("sha1 must be 40 lowercase hex chars")


def _require_edit(edit: GooglePlayEdit) -> None:
    if edit is None or edit.edit_id is None:
        raise PlayEditError(
            "update_track / upload_bundle require an open GooglePlayEdit",
        )


def _validate_rollout(
    *,
    track: Track,
    status: TrackStatus,
    user_fraction: float,
) -> None:
    if not (0.0 <= user_fraction <= 1.0):
        raise PlayRolloutError(
            f"user_fraction must be in [0, 1]; got {user_fraction}",
        )
    if status is TrackStatus.in_progress:
        if track not in STAGED_ROLLOUT_TRACKS:
            raise PlayRolloutError(
                f"inProgress status requires a staged-rollout track "
                f"(one of {sorted(t.value for t in STAGED_ROLLOUT_TRACKS)}); "
                f"track={track.value}",
            )
        if user_fraction >= 1.0:
            raise PlayRolloutError(
                "inProgress rollout with user_fraction=1.0 is invalid; "
                "use TrackStatus.completed instead",
            )
        if user_fraction <= 0.0:
            raise PlayRolloutError(
                "inProgress rollout requires user_fraction > 0",
            )
    elif status is TrackStatus.completed:
        if user_fraction != 1.0:
            raise PlayRolloutError(
                "completed status requires user_fraction=1.0",
            )
    elif status is TrackStatus.halted:
        if not (0.0 <= user_fraction <= 1.0):  # pragma: no cover
            raise PlayRolloutError("halted rollout requires user_fraction in [0, 1]")


def _rand_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


def _raise_for_status(
    resp: TransportResponse,
    *,
    op: str,
    typ: type[GooglePlayError] = GooglePlayError,
) -> None:
    if resp.ok():
        return
    err = resp.body.get("error")
    if isinstance(err, dict):
        raise typ(
            f"Play {op} failed ({resp.status}): "
            f"{err.get('status', '')} {err.get('message', '')}",
        )
    raise typ(f"Play {op} failed ({resp.status}): {resp.body!r}")


def _require_dual_sign_if_enforced(ctx: Any | None, *, op: str) -> None:
    if not _ENFORCE_DUAL_SIGN:
        return
    _require_dual_sign_strict(ctx, op=op)


def _require_dual_sign_strict_if_production(
    *, track: Track, ctx: Any | None,
) -> None:
    if track is not Track.production:
        return
    _require_dual_sign_strict(ctx, op="update_track(production)")


def _require_dual_sign_strict(ctx: Any | None, *, op: str) -> None:
    if ctx is None:
        raise PlayMissingDualSignError(
            f"Play {op} requires an O7 dual-sign context "
            "(Merger +2 + Human +2).",
        )
    allow = getattr(ctx, "allow", None)
    if allow is None and isinstance(ctx, dict):
        allow = ctx.get("allow")
    if not allow:
        reason = getattr(ctx, "reason", None) or (
            ctx.get("reason") if isinstance(ctx, dict) else None
        )
        raise PlayMissingDualSignError(
            f"Play {op} dual-sign context is not in allow state "
            f"(reason={reason!r}).",
        )


def _extract_audit_entry(ctx: Any | None) -> dict[str, Any]:
    if ctx is None:
        return {}
    if hasattr(ctx, "audit_entry"):
        return dict(getattr(ctx, "audit_entry") or {})
    if isinstance(ctx, dict) and "audit_entry" in ctx:
        return dict(ctx["audit_entry"] or {})
    return {}


_ENFORCE_DUAL_SIGN: bool = False


def set_enforce_dual_sign(flag: bool) -> None:
    global _ENFORCE_DUAL_SIGN
    _ENFORCE_DUAL_SIGN = bool(flag)


def redacted_summary(credentials: GooglePlayCredentials) -> dict[str, str]:
    return credentials.redacted()


__all__ = [
    "FakeTransport",
    "GooglePlayClient",
    "GooglePlayCredentials",
    "GooglePlayEdit",
    "GooglePlayError",
    "HttpTransport",
    "PLAY_DEVELOPER_BASE_URL",
    "PLAY_JWT_ALGORITHM",
    "PLAY_SCOPE",
    "PLAY_TOKEN_LIFETIME",
    "PLAY_TOKEN_URL",
    "PlayAuthError",
    "PlayEditError",
    "PlayInvalidCredentialsError",
    "PlayMissingDualSignError",
    "PlayRolloutError",
    "STAGED_ROLLOUT_TRACKS",
    "Track",
    "TrackStatus",
    "TrackUpdate",
    "Transport",
    "TransportResponse",
    "UploadedBundle",
    "issue_service_account_jwt",
    "redacted_summary",
    "set_enforce_dual_sign",
]
