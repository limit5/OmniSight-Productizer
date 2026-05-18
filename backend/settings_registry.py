"""WP.6 (OP-1500) — Settings sync-scope registry.

Pattern inspired by Warp ``crates/settings/src/lib.rs`` +
``define_settings_group!`` macro; **independently implemented**, no
Warp source consulted or vendored (inspiration-only tier — see
``docs/legal/oss-boundaries.md``).

Every setting that flows through the J4 ``user_preferences`` table
gets three pieces of metadata declared here:

* ``scope`` — ``tenant`` / ``user`` / ``device``: who the value
  belongs to. (Today the J4 table is keyed by ``(user_id, pref_key)``
  with a ``tenant_id`` column for RLS, so the ``tenant`` / ``user``
  distinction is informational at this layer. ``device`` matters
  because it disables cross-device broadcast.)
* ``sync`` — ``globally`` / ``per_platform`` / ``never``: how
  changes propagate across the user's devices.
* ``supported_platforms`` — concrete platforms the setting makes
  sense on (an empty tuple means "all").

The sync mode controls **two** runtime behaviours:

1. The effective J4 ``pref_key`` written to the table — global is the
   bare key, per-platform mints ``<key>@platform=<p>``, never mints
   ``<key>@device=<device_id>``. Key-shape namespacing avoids any
   ``user_preferences`` PK migration (the existing
   ``(user_id, pref_key)`` PK still partitions cleanly).
2. The cross-device broadcast — ``sync='never'`` suppresses
   ``emit_preferences_updated`` so sibling devices never even see
   the write (matches Warp's "machine-local" semantics for
   bench-target / sandbox-host-binding style settings).

The registry is also **persisted** in the ``settings_registry`` PG
table (alembic 0244) so it's queryable from DB tooling without
loading the Python module; the seed rows mirror this module
verbatim, and ``rebuild_registry_table()`` re-syncs them on
startup.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

# ─── Type vocabulary ───────────────────────────────────────────────

Scope = Literal["tenant", "user", "device"]
SyncMode = Literal["globally", "per_platform", "never"]

SCOPE_VALUES: tuple[Scope, ...] = ("tenant", "user", "device")
SYNC_MODE_VALUES: tuple[SyncMode, ...] = ("globally", "per_platform", "never")

# Platform vocabulary — kept deliberately small. Mirrors the
# detection helper :func:`derive_platform_from_user_agent` below;
# adding a new platform here requires adding the matching UA-sniff
# clause there.
Platform = Literal["macos", "windows", "linux", "ios", "android", "web"]

PLATFORM_VALUES: tuple[Platform, ...] = (
    "macos", "windows", "linux", "ios", "android", "web",
)


@dataclass(frozen=True)
class SettingMetadata:
    """Sync-scope metadata for a single J4 ``user_preferences`` key.

    Frozen dataclass so the registry can be hashed / safely shared
    across async tasks without copy. ``supported_platforms=()`` means
    "every platform" (the registry never enforces against an empty
    tuple).
    """

    pref_key: str
    scope: Scope
    sync: SyncMode
    supported_platforms: tuple[Platform, ...] = field(default_factory=tuple)
    description: str = ""
    default_value: str = ""

    def supports_platform(self, platform: Platform | str) -> bool:
        if not self.supported_platforms:
            return True
        return platform in self.supported_platforms


# ─── Initial registry seed ─────────────────────────────────────────
# Each existing J4 key we know about gets a row here. Future settings
# should be added at the same time the frontend SoT module is
# introduced (e.g. ``lib/motion-preferences.ts`` adds an entry when
# the WP.6 fold reaches its referenced surface).

_INITIAL_SETTINGS: tuple[SettingMetadata, ...] = (
    # BS.3.3 — motion level: visual preference, applies the same on
    # every device the user owns. ``globally`` keeps a phone + laptop
    # in lock-step the moment the user picks ``subtle`` anywhere.
    SettingMetadata(
        pref_key="motion_level",
        scope="user",
        sync="globally",
        supported_platforms=(),
        description="Motion-effect intensity (off / subtle / normal / dramatic).",
        default_value="dramatic",
    ),
    # BS.11.4 — catalog density: same as motion, a visual preference
    # that should follow the user. Global.
    SettingMetadata(
        pref_key="catalog_density",
        scope="user",
        sync="globally",
        supported_platforms=(),
        description="Catalog card density (compact / comfortable / spacious).",
        default_value="comfortable",
    ),
    # WP.4 — onboarding journey picker. The chosen journey is
    # universally relevant; a second device should skip the picker.
    SettingMetadata(
        pref_key="onboarding_intention",
        scope="user",
        sync="globally",
        supported_platforms=(),
        description="WP.4 first-run intention picker selection.",
        default_value="",
    ),
    # J4 — onboarding tour seen marker. Once you've seen the tour on
    # any device, you don't need to see it on another.
    SettingMetadata(
        pref_key="tour_seen",
        scope="user",
        sync="globally",
        supported_platforms=(),
        description="Whether the first-run onboarding tour has been dismissed.",
        default_value="0",
    ),
    # Theme — Light / Dark / system. Visual preference, global.
    SettingMetadata(
        pref_key="theme",
        scope="user",
        sync="globally",
        supported_platforms=(),
        description="UI theme (light / dark / system).",
        default_value="system",
    ),
    # WP.6 example — notification_sound is per-platform because the
    # available sounds differ (macOS bundled sounds ≠ Windows ≠
    # Linux). A phone vs. laptop on the same platform still shares.
    SettingMetadata(
        pref_key="notification_sound",
        scope="user",
        sync="per_platform",
        supported_platforms=("macos", "windows", "linux"),
        description="Notification sound (per-platform sound library).",
        default_value="",
    ),
    # WP.6 example — keybindings differ per OS (Cmd vs. Ctrl). The
    # default chord on macOS shouldn't override the laptop on Linux.
    SettingMetadata(
        pref_key="keybindings_profile",
        scope="user",
        sync="per_platform",
        supported_platforms=("macos", "windows", "linux", "web"),
        description="Keybindings profile (per-platform — modifier keys differ).",
        default_value="default",
    ),
    # WP.6 example — HD bring-up bench target is a physical device
    # plugged into one specific machine. Cross-device sync would
    # actively break the workbench.
    SettingMetadata(
        pref_key="hardware_bench_target",
        scope="device",
        sync="never",
        supported_platforms=("macos", "windows", "linux"),
        description="HD bring-up bench target (machine-local hardware).",
        default_value="",
    ),
    # WP.6 example — sandbox host binding is also machine-local.
    SettingMetadata(
        pref_key="sandbox_host_binding",
        scope="device",
        sync="never",
        supported_platforms=("macos", "windows", "linux"),
        description="W14 sandbox host binding (per-machine).",
        default_value="",
    ),
)


# ─── Registry storage (dict for O(1) lookup) ───────────────────────

_REGISTRY: dict[str, SettingMetadata] = {
    meta.pref_key: meta for meta in _INITIAL_SETTINGS
}


def metadata_for(pref_key: str) -> SettingMetadata | None:
    """Look up the sync-scope metadata for a base pref key.

    Returns ``None`` for keys not declared in the registry — callers
    treat unregistered keys as legacy (globally synced, user-scoped,
    all platforms) so adding a new pref doesn't break on Day 1.
    """
    return _REGISTRY.get(pref_key)


def list_settings() -> list[SettingMetadata]:
    """All registered settings, in declaration order."""
    return list(_REGISTRY.values())


def supported_pref_keys() -> tuple[str, ...]:
    """Stable tuple of declared base pref_keys (test helper)."""
    return tuple(_REGISTRY.keys())


# ─── Partitioned key shape ─────────────────────────────────────────

KEY_PLATFORM_SEP = "@platform="
KEY_DEVICE_SEP = "@device="


def partition_key(
    pref_key: str,
    *,
    platform: Platform | str | None = None,
    device_id: str | None = None,
) -> str:
    """Mint the effective J4 ``pref_key`` for a settings-registry write.

    The base ``pref_key`` is namespaced with the partition that the
    setting's :class:`SettingMetadata.sync` mode demands:

    * ``globally`` → the base key, unchanged.
    * ``per_platform`` → ``<key>@platform=<p>`` (e.g.
      ``notification_sound@platform=macos``). The frontend reads/writes
      against this partitioned key; sibling devices on the same
      platform converge through the existing J4 PG row.
    * ``never`` → ``<key>@device=<device_id>``. No cross-device
      broadcast (see :func:`should_broadcast`), so the row is read
      back only by the originating device.

    Unregistered keys behave as ``globally``: the caller's bare key is
    returned. This keeps legacy callers working without a registry
    entry — they degrade to today's behaviour.
    """
    meta = metadata_for(pref_key)
    if meta is None:
        return pref_key
    if meta.sync == "globally":
        return pref_key
    if meta.sync == "per_platform":
        if not platform:
            raise ValueError(
                f"setting {pref_key!r} has sync=per_platform; "
                "platform= is required"
            )
        if not meta.supports_platform(platform):
            raise ValueError(
                f"setting {pref_key!r} does not support platform {platform!r}; "
                f"supported: {meta.supported_platforms}"
            )
        return f"{pref_key}{KEY_PLATFORM_SEP}{platform}"
    if meta.sync == "never":
        if not device_id:
            raise ValueError(
                f"setting {pref_key!r} has sync=never; device_id= is required"
            )
        return f"{pref_key}{KEY_DEVICE_SEP}{device_id}"
    raise AssertionError(f"unhandled sync mode for {pref_key!r}: {meta.sync!r}")


def parse_partitioned_key(stored_key: str) -> tuple[str, str | None, str | None]:
    """Inverse of :func:`partition_key`.

    Returns ``(base_key, platform_or_none, device_or_none)`` so
    consumers iterating the raw ``user_preferences`` rows can group
    them by base key. Bare keys (no ``@platform=`` / ``@device=``
    suffix) return ``(stored_key, None, None)``.
    """
    if KEY_PLATFORM_SEP in stored_key:
        base, _, platform = stored_key.partition(KEY_PLATFORM_SEP)
        return base, platform or None, None
    if KEY_DEVICE_SEP in stored_key:
        base, _, device = stored_key.partition(KEY_DEVICE_SEP)
        return base, None, device or None
    return stored_key, None, None


def should_broadcast(pref_key: str) -> bool:
    """Suppress cross-device emit for ``sync='never'`` settings.

    Wired into ``backend/routers/preferences.py`` so the
    ``preferences.updated`` SSE event never fires for device-local
    rows. Sibling devices stay completely unaware of the write;
    matches Warp's "machine-local" behaviour for bench / hardware
    bindings.
    """
    meta = metadata_for(pref_key)
    if meta is None:
        return True  # legacy / unregistered: default to broadcast.
    return meta.sync != "never"


# ─── User-Agent → platform sniff ───────────────────────────────────

def derive_platform_from_user_agent(user_agent: str) -> Platform:
    """Best-effort platform detection from a browser User-Agent string.

    Used by ``backend/routers/preferences.py`` to fill in the
    ``?platform=`` query param when the caller didn't supply one
    (clients in practice almost always reach the API through a
    browser session, so UA-sniff is more pragmatic than asking every
    UI to wire an explicit platform header).

    The fallback is ``"web"`` so an unparseable / empty UA never
    blocks a per-platform write — it lands in a stable "web" bucket
    that the frontend can opt to share or override.
    """
    if not user_agent:
        return "web"
    ua = user_agent.lower()
    # Order matters: iPad / iPhone before Mac, Android before Linux.
    if "iphone" in ua or "ipad" in ua or "ipod" in ua:
        return "ios"
    if "android" in ua:
        return "android"
    if "mac os" in ua or "macintosh" in ua:
        return "macos"
    if "windows" in ua:
        return "windows"
    if "linux" in ua:
        return "linux"
    return "web"


# ─── Public-facing dict view (for /settings/registry JSON) ─────────

def to_public_view() -> list[dict]:
    """Stable JSON-friendly representation of the registry.

    Mirrors the shape the frontend ``lib/settings-registry.ts``
    consumer expects: list of ``{pref_key, scope, sync,
    supported_platforms, description, default_value}`` dicts. Order
    matches declaration order so the operator-facing UI list is
    deterministic.
    """
    return [
        {
            "pref_key": m.pref_key,
            "scope": m.scope,
            "sync": m.sync,
            "supported_platforms": list(m.supported_platforms),
            "description": m.description,
            "default_value": m.default_value,
        }
        for m in _REGISTRY.values()
    ]


# ─── Optional persistence sync (DB write-through) ──────────────────

async def rebuild_registry_table() -> None:
    """Idempotent upsert of the Python registry into ``settings_registry``.

    Called once at startup so DB tooling / Grafana queries can see the
    same rows the Python code knows about. Safe to call repeatedly —
    every row is an ``INSERT ... ON CONFLICT DO UPDATE``. Failures are
    swallowed (logged), never fatal: the in-memory registry is the
    source of truth for the runtime path; the DB table is a mirror.
    """
    import json
    import logging
    import time

    log = logging.getLogger(__name__)

    try:
        from backend.db_pool import get_pool
        pool = get_pool()
    except Exception as exc:
        log.debug("settings_registry: pool unavailable, skip rebuild: %s", exc)
        return

    now = time.time()
    rows = [
        (
            m.pref_key,
            m.scope,
            m.sync,
            json.dumps(list(m.supported_platforms)),
            m.description,
            m.default_value,
            now,
        )
        for m in _REGISTRY.values()
    ]
    try:
        async with pool.acquire() as conn:
            await conn.executemany(
                "INSERT INTO settings_registry "
                "(pref_key, scope, sync_mode, supported_platforms, "
                " description, default_value, updated_at) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7) "
                "ON CONFLICT (pref_key) DO UPDATE SET "
                "  scope = EXCLUDED.scope, "
                "  sync_mode = EXCLUDED.sync_mode, "
                "  supported_platforms = EXCLUDED.supported_platforms, "
                "  description = EXCLUDED.description, "
                "  default_value = EXCLUDED.default_value, "
                "  updated_at = EXCLUDED.updated_at",
                rows,
            )
    except Exception as exc:
        log.debug("settings_registry rebuild failed (non-fatal): %s", exc)
