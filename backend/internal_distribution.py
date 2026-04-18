"""P5 (#290) — Internal distribution (TestFlight + Firebase App Distribution).

Ships pre-release / nightly builds to QA, stakeholders, and
contractors without touching the public stores.  The module exposes
two thin clients and a unified :class:`InternalDistributionManager`
that routes an incoming build to the right surface based on its
platform.

Design
------
* **Reuses the ASC + Play transport abstractions.**  TestFlight is a
  sub-API under App Store Connect (``/v1/betaAppReviewSubmissions``,
  ``/v1/betaGroups``); Firebase App Distribution uses the Firebase
  Management API (``firebaseappdistribution.googleapis.com``).  Both
  accept the :class:`Transport` interface so offline tests stay
  trivial.
* **Tester groups as first-class objects.**  The same internal team
  list is reused across nightly builds, so
  :class:`TesterGroup` models the membership (``external_email_list``
  for TF external testers, ``groupAlias`` for Firebase) and the
  distribution call references it by id.
* **Merger-+2-only gate.**  Per
  :data:`backend.store_submission.TARGETS_REQUIRING_HUMAN`, internal
  targets ship with just the Merger Agent's +2 — the human-guideline
  review is deferred to the eventual store-facing submission.
* **Release notes are mandatory but short.**  TF rejects a build with
  empty ``whatToTest`` strings; Firebase silently ships but testers
  get zero context.  We enforce 1..4000 chars at the coordinator.

Public API
----------
``TestFlightClient`` / ``FirebaseAppDistributionClient``
``TesterGroup``
``InternalDistribution``
``InternalDistributionManager``
``distribute_internal(...)``
"""

from __future__ import annotations

import enum
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from backend.app_store_connect import (
    AppStoreCredentials,
    HttpTransport,
    Transport,
    TransportResponse,
    issue_jwt,
)
from backend.google_play_developer import (
    GooglePlayCredentials,
    issue_service_account_jwt,
)

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  0. Types
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class InternalDistributionError(Exception):
    pass


class DistributionPlatform(str, enum.Enum):
    ios = "ios"
    android = "android"


EMAIL_PATTERN = re.compile(
    r"^[A-Za-z0-9_.+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+$",
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  1. Tester group
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass(frozen=True)
class TesterGroup:
    """Unified tester-group model for TF + Firebase.

    ``platform`` decides which fields matter — TF uses beta group ids +
    email lists, Firebase uses group aliases.  The coordinator validates
    that the fields required by the target platform are present.
    """

    group_id: str
    name: str
    platform: DistributionPlatform
    emails: tuple[str, ...] = ()
    alias: str = ""  # firebase group alias (e.g. "qa-internal")

    def __post_init__(self) -> None:
        if not self.group_id:
            raise InternalDistributionError("group_id required")
        for email in self.emails:
            if not EMAIL_PATTERN.match(email):
                raise InternalDistributionError(
                    f"invalid email in tester group: {email!r}",
                )
        if self.platform is DistributionPlatform.ios and not self.emails:
            raise InternalDistributionError(
                "iOS tester group (TestFlight) requires at least 1 email",
            )
        if self.platform is DistributionPlatform.android and not self.alias:
            raise InternalDistributionError(
                "Android tester group (Firebase) requires an alias",
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "group_id": self.group_id,
            "name": self.name,
            "platform": self.platform.value,
            "emails": list(self.emails),
            "alias": self.alias,
        }


@dataclass(frozen=True)
class InternalDistribution:
    """Record of a successful internal-distribution dispatch."""

    distribution_id: str
    platform: DistributionPlatform
    build_id: str
    group_ids: tuple[str, ...]
    release_notes: str
    distributed_at: float
    audit_entry: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "distribution_id": self.distribution_id,
            "platform": self.platform.value,
            "build_id": self.build_id,
            "group_ids": list(self.group_ids),
            "release_notes": self.release_notes,
            "distributed_at": self.distributed_at,
            "audit_entry": dict(self.audit_entry),
        }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  2. TestFlight client (subset of ASC)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


TESTFLIGHT_BASE_URL: str = "https://api.appstoreconnect.apple.com"


class TestFlightClient:
    """Thin ASC subset for beta-app distribution.

    Shares the :class:`AppStoreCredentials` with the full ASC client so
    one JWT issuer covers both store-facing and internal flows.
    """

    def __init__(
        self,
        credentials: AppStoreCredentials,
        *,
        transport: Transport | None = None,
        base_url: str = TESTFLIGHT_BASE_URL,
        signer: Callable[[bytes, str], bytes] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.credentials = credentials
        self.transport = transport or HttpTransport()
        self.base_url = base_url.rstrip("/")
        self._signer = signer
        self._clock = clock or time.time
        self._jwt: dict[str, Any] | None = None

    def _auth_headers(self) -> dict[str, str]:
        now = self._clock()
        if (
            self._jwt is None
            or self._jwt.get("expires_at", 0.0) - now < 60.0
        ):
            self._jwt = issue_jwt(
                self.credentials, now=now, signer=self._signer,
            )
        return {
            "Authorization": f"Bearer {self._jwt['token']}",
            "Accept": "application/json",
        }

    def create_beta_group(
        self,
        *,
        group_name: str,
        is_internal: bool = True,
    ) -> dict[str, Any]:
        """Create a TestFlight beta group.  Returns the raw group id + name."""
        if not group_name or len(group_name) > 50:
            raise InternalDistributionError(
                "beta group_name must be 1..50 chars",
            )
        resp = self.transport.request(
            method="POST",
            url=f"{self.base_url}/v1/betaGroups",
            headers=self._auth_headers(),
            json_body={
                "data": {
                    "type": "betaGroups",
                    "attributes": {
                        "name": group_name,
                        "isInternalGroup": is_internal,
                    },
                    "relationships": {
                        "app": {
                            "data": {
                                "type": "apps",
                                "id": self.credentials.app_id or "unknown",
                            },
                        },
                    },
                },
            },
        )
        _raise_for_status(resp, op="tf.create_beta_group")
        data = resp.body.get("data", {})
        return {
            "group_id": str(data.get("id") or _rand_id("bgp")),
            "name": group_name,
            "internal": is_internal,
        }

    def distribute_to_group(
        self,
        *,
        build_id: str,
        group_id: str,
        what_to_test: str,
        dual_sign_context: Any,
    ) -> dict[str, Any]:
        """Assign a build to a beta group and set the ``whatToTest`` note."""
        _require_dual_sign(dual_sign_context, op="tf.distribute_to_group")
        _require_whats_new(what_to_test)
        resp = self.transport.request(
            method="POST",
            url=f"{self.base_url}/v1/betaBuildLocalizations",
            headers=self._auth_headers(),
            json_body={
                "data": {
                    "type": "betaBuildLocalizations",
                    "attributes": {"whatsNew": what_to_test, "locale": "en-US"},
                    "relationships": {
                        "build": {
                            "data": {
                                "type": "builds",
                                "id": build_id,
                            },
                        },
                    },
                },
            },
        )
        _raise_for_status(resp, op="tf.betaBuildLocalizations")
        resp2 = self.transport.request(
            method="POST",
            url=f"{self.base_url}/v1/betaGroups/{group_id}/relationships/builds",
            headers=self._auth_headers(),
            json_body={
                "data": [{"type": "builds", "id": build_id}],
            },
        )
        _raise_for_status(resp2, op="tf.betaGroups.builds")
        return {
            "build_id": build_id,
            "group_id": group_id,
            "what_to_test": what_to_test,
        }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  3. Firebase App Distribution client
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


FIREBASE_APP_DISTRIBUTION_BASE_URL: str = (
    "https://firebaseappdistribution.googleapis.com"
)
FIREBASE_SCOPE: str = (
    "https://www.googleapis.com/auth/cloud-platform"
)


class FirebaseAppDistributionClient:
    """Firebase App Distribution v1 façade.

    Expects a :class:`GooglePlayCredentials`-shaped service account —
    reusing the type so a single set of Google creds drives both Play
    + Firebase.  ``package_name`` here is the Firebase ``appId``
    (``1:123:android:abc``-shaped); we don't reparse it — the caller
    supplies the raw appId in ``firebase_app_id``.
    """

    def __init__(
        self,
        credentials: GooglePlayCredentials,
        *,
        firebase_app_id: str,
        transport: Transport | None = None,
        base_url: str = FIREBASE_APP_DISTRIBUTION_BASE_URL,
        signer: Callable[[bytes, str], bytes] | None = None,
        clock: Callable[[], float] | None = None,
        token_exchange: Callable[[str, str], dict[str, Any]] | None = None,
    ) -> None:
        if not firebase_app_id:
            raise InternalDistributionError("firebase_app_id required")
        self.credentials = credentials
        self.firebase_app_id = firebase_app_id
        self.transport = transport or HttpTransport()
        self.base_url = base_url.rstrip("/")
        self._signer = signer
        self._clock = clock or time.time
        self._access_token: dict[str, Any] | None = None
        self._token_exchange = token_exchange

    def _auth_headers(self) -> dict[str, str]:
        now = self._clock()
        if (
            self._access_token is None
            or self._access_token.get("expires_at", 0.0) - now < 60.0
        ):
            bundle = issue_service_account_jwt(
                self.credentials, now=now,
                scope=FIREBASE_SCOPE, signer=self._signer,
            )
            if self._token_exchange is not None:
                self._access_token = self._token_exchange(
                    bundle["assertion"], self.credentials.token_uri,
                )
            else:
                # Offline fallback — mark the assertion as the access
                # token so tests don't need to mock the exchange.
                self._access_token = {
                    "access_token": bundle["assertion"],
                    "expires_at": bundle["expires_at"],
                }
        return {
            "Authorization": f"Bearer {self._access_token['access_token']}",
            "Accept": "application/json",
        }

    def distribute(
        self,
        *,
        release_id: str,
        group_aliases: Sequence[str],
        tester_emails: Sequence[str],
        release_notes: str,
        dual_sign_context: Any,
    ) -> dict[str, Any]:
        _require_dual_sign(dual_sign_context, op="firebase.distribute")
        _require_whats_new(release_notes)
        aliases = list(group_aliases or ())
        emails = list(tester_emails or ())
        if not aliases and not emails:
            raise InternalDistributionError(
                "firebase.distribute needs at least one group alias or email",
            )
        for e in emails:
            if not EMAIL_PATTERN.match(e):
                raise InternalDistributionError(
                    f"invalid tester email: {e!r}",
                )
        resp = self.transport.request(
            method="POST",
            url=(
                f"{self.base_url}/v1/projects/-/apps/{self.firebase_app_id}"
                f"/releases/{release_id}:distribute"
            ),
            headers=self._auth_headers(),
            json_body={
                "testerEmails": emails,
                "groupAliases": aliases,
            },
        )
        _raise_for_status(resp, op="firebase.distribute")
        if release_notes:
            self.transport.request(
                method="PATCH",
                url=(
                    f"{self.base_url}/v1/projects/-/apps/{self.firebase_app_id}"
                    f"/releases/{release_id}?updateMask=releaseNotes.text"
                ),
                headers=self._auth_headers(),
                json_body={"releaseNotes": {"text": release_notes}},
            )
        return {
            "release_id": release_id,
            "firebase_app_id": self.firebase_app_id,
            "group_aliases": aliases,
            "tester_emails": emails,
        }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  4. Unified manager
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass
class InternalDistributionManager:
    """Routes a build to the right internal channel by platform.

    Stores :class:`TesterGroup` entries keyed by ``group_id`` plus
    optional TF / Firebase clients.  ``distribute`` picks the client by
    the build's platform and returns an :class:`InternalDistribution`.
    """

    testflight: TestFlightClient | None = None
    firebase: FirebaseAppDistributionClient | None = None
    groups: dict[str, TesterGroup] = field(default_factory=dict)
    history: list[InternalDistribution] = field(default_factory=list)

    def register_group(self, group: TesterGroup) -> TesterGroup:
        if group.group_id in self.groups:
            raise InternalDistributionError(
                f"tester group already registered: {group.group_id}",
            )
        self.groups[group.group_id] = group
        return group

    def get_group(self, group_id: str) -> TesterGroup:
        g = self.groups.get(group_id)
        if g is None:
            raise InternalDistributionError(
                f"unknown tester group: {group_id}",
            )
        return g

    def distribute(
        self,
        *,
        platform: DistributionPlatform | str,
        build_id: str,
        group_ids: Sequence[str],
        release_notes: str,
        dual_sign_context: Any,
    ) -> InternalDistribution:
        plat = _coerce_platform(platform)
        _require_whats_new(release_notes)
        _require_dual_sign(dual_sign_context, op=f"internal.{plat.value}")
        if not group_ids:
            raise InternalDistributionError("group_ids must not be empty")
        groups = [self.get_group(gid) for gid in group_ids]
        for g in groups:
            if g.platform is not plat:
                raise InternalDistributionError(
                    f"tester group {g.group_id} is {g.platform.value}, "
                    f"does not match build platform {plat.value}",
                )
        if plat is DistributionPlatform.ios:
            if self.testflight is None:
                raise InternalDistributionError(
                    "TestFlight client not configured",
                )
            for g in groups:
                self.testflight.distribute_to_group(
                    build_id=build_id,
                    group_id=g.group_id,
                    what_to_test=release_notes,
                    dual_sign_context=dual_sign_context,
                )
        else:
            if self.firebase is None:
                raise InternalDistributionError(
                    "Firebase App Distribution client not configured",
                )
            # Flatten across groups for a single distribute call.
            aliases = [g.alias for g in groups if g.alias]
            emails: list[str] = []
            for g in groups:
                emails.extend(g.emails)
            self.firebase.distribute(
                release_id=build_id,
                group_aliases=aliases,
                tester_emails=emails,
                release_notes=release_notes,
                dual_sign_context=dual_sign_context,
            )
        record = InternalDistribution(
            distribution_id=f"dist-{uuid.uuid4().hex[:16]}",
            platform=plat,
            build_id=build_id,
            group_ids=tuple(g.group_id for g in groups),
            release_notes=release_notes,
            distributed_at=(
                self.testflight._clock() if self.testflight is not None
                else (self.firebase._clock() if self.firebase is not None
                      else time.time())
            ),
            audit_entry=_extract_audit_entry(dual_sign_context),
        )
        self.history.append(record)
        return record


def distribute_internal(
    *,
    manager: InternalDistributionManager,
    platform: DistributionPlatform | str,
    build_id: str,
    group_ids: Sequence[str],
    release_notes: str,
    dual_sign_context: Any,
) -> InternalDistribution:
    """Module-level convenience wrapper."""
    return manager.distribute(
        platform=platform,
        build_id=build_id,
        group_ids=group_ids,
        release_notes=release_notes,
        dual_sign_context=dual_sign_context,
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  5. Helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _coerce_platform(
    platform: DistributionPlatform | str,
) -> DistributionPlatform:
    if isinstance(platform, DistributionPlatform):
        return platform
    try:
        return DistributionPlatform(platform)
    except ValueError as exc:
        raise InternalDistributionError(
            f"unknown platform {platform!r}; expected 'ios' or 'android'",
        ) from exc


def _require_whats_new(text: str) -> None:
    if not text or not text.strip():
        raise InternalDistributionError(
            "release_notes / what_to_test must not be blank",
        )
    if len(text) > 4000:
        raise InternalDistributionError(
            f"release_notes too long ({len(text)} > 4000 chars)",
        )


def _require_dual_sign(ctx: Any | None, *, op: str) -> None:
    if ctx is None:
        raise InternalDistributionError(
            f"{op} requires a dual-sign context (Merger +2 at minimum)",
        )
    allow = getattr(ctx, "allow", None)
    if allow is None and isinstance(ctx, dict):
        allow = ctx.get("allow")
    if not allow:
        reason = getattr(ctx, "reason", None) or (
            ctx.get("reason") if isinstance(ctx, dict) else None
        )
        raise InternalDistributionError(
            f"{op} dual-sign context is not in allow state (reason={reason!r})",
        )


def _extract_audit_entry(ctx: Any | None) -> dict[str, Any]:
    if ctx is None:
        return {}
    if hasattr(ctx, "audit_entry"):
        return dict(getattr(ctx, "audit_entry") or {})
    if isinstance(ctx, dict) and "audit_entry" in ctx:
        return dict(ctx["audit_entry"] or {})
    return {}


def _raise_for_status(resp: TransportResponse, *, op: str) -> None:
    if resp.ok():
        return
    raise InternalDistributionError(
        f"{op} failed ({resp.status}): {resp.body!r}",
    )


def _rand_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


__all__ = [
    "DistributionPlatform",
    "FIREBASE_APP_DISTRIBUTION_BASE_URL",
    "FIREBASE_SCOPE",
    "FirebaseAppDistributionClient",
    "InternalDistribution",
    "InternalDistributionError",
    "InternalDistributionManager",
    "TESTFLIGHT_BASE_URL",
    "TesterGroup",
    "TestFlightClient",
    "distribute_internal",
]
