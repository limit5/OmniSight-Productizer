"""WP.6 (OP-1500) — Settings sync-scope registry tests.

Locks the three sync-mode behaviours we depend on:

1. ``globally`` — bare pref_key, broadcast fires.
2. ``per_platform`` — pref_key minted with ``@platform=<p>``;
   different platforms partition into separate rows; same-platform
   sibling devices converge through the shared row.
3. ``never`` — pref_key minted with ``@device=<id>``; the
   ``emit_preferences_updated`` broadcast is suppressed entirely so
   sibling devices never see the write.

Plus contract tests on the registry shape (lookup, validation,
UA-sniff, public view) and the new ``/settings/registry`` GET
endpoint shape.

The HTTP-driven sub-tests reuse the ``_prefs_client`` fixture
pattern from ``test_user_preferences.py``.
"""
from __future__ import annotations

import pytest
from httpx import AsyncClient, ASGITransport

from backend import settings_registry as _settings_registry


# ─── Pure-Python registry tests ────────────────────────────────────


class TestRegistryShape:
    def test_initial_registry_contains_known_keys(self):
        keys = _settings_registry.supported_pref_keys()
        # Lock the seeded keys; if a future commit drops one of these
        # without adding a migration, this test fails so the omission
        # is visible.
        for required in (
            "motion_level",
            "catalog_density",
            "onboarding_intention",
            "tour_seen",
            "theme",
            "notification_sound",
            "keybindings_profile",
            "hardware_bench_target",
            "sandbox_host_binding",
        ):
            assert required in keys, f"{required} missing from registry"

    def test_metadata_for_unknown_key_returns_none(self):
        assert _settings_registry.metadata_for("not_a_real_key") is None

    def test_metadata_for_known_key_has_required_fields(self):
        meta = _settings_registry.metadata_for("motion_level")
        assert meta is not None
        assert meta.scope in _settings_registry.SCOPE_VALUES
        assert meta.sync in _settings_registry.SYNC_MODE_VALUES
        # motion_level is globally synced — every device should follow
        assert meta.sync == "globally"

    def test_to_public_view_is_json_friendly(self):
        view = _settings_registry.to_public_view()
        assert isinstance(view, list)
        for entry in view:
            assert set(entry.keys()) == {
                "pref_key", "scope", "sync",
                "supported_platforms", "description", "default_value",
            }
            assert isinstance(entry["supported_platforms"], list)


class TestPartitionKey:
    def test_globally_synced_returns_bare_key(self):
        assert (
            _settings_registry.partition_key("motion_level") == "motion_level"
        )

    def test_globally_synced_ignores_platform_and_device(self):
        # Defensive: even if a caller passes spurious args, the global
        # setting still resolves to the bare key (registry decides).
        assert (
            _settings_registry.partition_key(
                "motion_level", platform="macos", device_id="abc",
            )
            == "motion_level"
        )

    def test_per_platform_requires_platform(self):
        with pytest.raises(ValueError, match="platform="):
            _settings_registry.partition_key("notification_sound")

    def test_per_platform_mints_suffixed_key(self):
        key = _settings_registry.partition_key(
            "notification_sound", platform="macos",
        )
        assert key == "notification_sound@platform=macos"

    def test_per_platform_rejects_unsupported_platform(self):
        # notification_sound supports macos / windows / linux only —
        # ios is not in supported_platforms.
        with pytest.raises(ValueError, match="does not support platform"):
            _settings_registry.partition_key(
                "notification_sound", platform="ios",
            )

    def test_never_requires_device_id(self):
        with pytest.raises(ValueError, match="device_id="):
            _settings_registry.partition_key("hardware_bench_target")

    def test_never_mints_device_suffixed_key(self):
        key = _settings_registry.partition_key(
            "hardware_bench_target", device_id="dev-abc-123",
        )
        assert key == "hardware_bench_target@device=dev-abc-123"

    def test_unregistered_key_returns_bare(self):
        assert (
            _settings_registry.partition_key("legacy_anything")
            == "legacy_anything"
        )

    def test_parse_round_trip_global(self):
        base, plat, dev = _settings_registry.parse_partitioned_key("motion_level")
        assert (base, plat, dev) == ("motion_level", None, None)

    def test_parse_round_trip_per_platform(self):
        key = _settings_registry.partition_key(
            "notification_sound", platform="macos",
        )
        base, plat, dev = _settings_registry.parse_partitioned_key(key)
        assert base == "notification_sound"
        assert plat == "macos"
        assert dev is None

    def test_parse_round_trip_device(self):
        key = _settings_registry.partition_key(
            "hardware_bench_target", device_id="dev-abc-123",
        )
        base, plat, dev = _settings_registry.parse_partitioned_key(key)
        assert base == "hardware_bench_target"
        assert plat is None
        assert dev == "dev-abc-123"


class TestShouldBroadcast:
    def test_globally_synced_broadcasts(self):
        assert _settings_registry.should_broadcast("motion_level") is True

    def test_per_platform_broadcasts(self):
        # per_platform still broadcasts — sibling devices on the same
        # platform must see updates. The partition is at the key level,
        # not the emit level.
        assert _settings_registry.should_broadcast("notification_sound") is True

    def test_never_suppresses_broadcast(self):
        assert (
            _settings_registry.should_broadcast("hardware_bench_target") is False
        )

    def test_unregistered_key_defaults_to_broadcast(self):
        # Legacy keys (not in registry) should keep the existing
        # behaviour: broadcast so other devices stay in sync.
        assert _settings_registry.should_broadcast("legacy_random_key") is True


class TestPlatformDetection:
    @pytest.mark.parametrize(
        "ua,expected",
        [
            ("Mozilla/5.0 (Macintosh; Intel Mac OS X 14_2)", "macos"),
            ("Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "windows"),
            ("Mozilla/5.0 (X11; Linux x86_64)", "linux"),
            ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0)", "ios"),
            ("Mozilla/5.0 (Linux; Android 14; Pixel 8)", "android"),
            ("", "web"),
            ("CustomBot/1.0", "web"),
        ],
    )
    def test_user_agent_to_platform(self, ua, expected):
        assert (
            _settings_registry.derive_platform_from_user_agent(ua) == expected
        )

    def test_ipad_detected_as_ios(self):
        ua = "Mozilla/5.0 (iPad; CPU OS 17_0 like Mac OS X)"
        # iPad has "Mac OS" in UA — make sure we don't misclassify.
        assert _settings_registry.derive_platform_from_user_agent(ua) == "ios"


# ─── HTTP round-trip tests (re-uses test_user_preferences fixture) ─


@pytest.fixture
async def _prefs_client(pg_test_pool, pg_test_dsn, monkeypatch):
    monkeypatch.setenv("OMNISIGHT_AUTH_MODE", "open")
    monkeypatch.setenv("OMNISIGHT_DATABASE_URL", pg_test_dsn)

    async with pg_test_pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE users, user_preferences RESTART IDENTITY CASCADE"
        )
        await conn.execute(
            "INSERT INTO users (id, email, name, role, password_hash, "
            "enabled, tenant_id) VALUES ($1, $2, $3, $4, $5, 1, $6) "
            "ON CONFLICT (id) DO NOTHING",
            "anonymous", "anonymous@local", "(anonymous)", "admin",
            "", "t-default",
        )

    from backend import db as _db
    from backend.main import app
    from backend import bootstrap as _boot

    async def _green():
        return _boot.BootstrapStatus(
            admin_password_default=False,
            llm_provider_configured=True,
            cf_tunnel_configured=True,
            smoke_passed=True,
        )
    monkeypatch.setattr(_boot, "get_bootstrap_status", _green)
    _boot._gate_cache_reset()

    if _db._db is not None:
        await _db.close()
    await _db.init()

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
    finally:
        _boot._gate_cache_reset()
        await _db.close()
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "TRUNCATE users, user_preferences RESTART IDENTITY CASCADE"
            )


@pytest.mark.asyncio
class TestSettingsRegistryEndpoint:
    async def test_get_returns_registry_payload(self, _prefs_client: AsyncClient):
        resp = await _prefs_client.get(
            "/api/v1/settings/registry",
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_2)"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "settings" in body
        assert "derived_platform" in body
        assert body["derived_platform"] == "macos"
        # Lock the vocabulary so a future enum rename triggers CI here.
        assert set(body["scope_values"]) == {"tenant", "user", "device"}
        assert set(body["sync_mode_values"]) == {
            "globally", "per_platform", "never",
        }
        # Sanity-check at least one entry has the full WP.6 shape.
        motion = next(
            (s for s in body["settings"] if s["pref_key"] == "motion_level"),
            None,
        )
        assert motion is not None
        assert motion["sync"] == "globally"
        assert motion["scope"] in ("tenant", "user", "device")

    async def test_per_platform_round_trip_partitions_by_platform(
        self, _prefs_client: AsyncClient,
    ):
        """WP.6 — same key, different platforms → two rows."""
        # macOS writes "marimba.aiff".
        put_mac = await _prefs_client.put(
            "/api/v1/user-preferences/notification_sound?platform=macos",
            json={"value": "marimba.aiff"},
        )
        assert put_mac.status_code == 200

        # Windows writes "chord.wav".
        put_win = await _prefs_client.put(
            "/api/v1/user-preferences/notification_sound?platform=windows",
            json={"value": "chord.wav"},
        )
        assert put_win.status_code == 200

        # macOS reads back "marimba.aiff", not "chord.wav".
        get_mac = await _prefs_client.get(
            "/api/v1/user-preferences/notification_sound?platform=macos",
        )
        assert get_mac.status_code == 200
        assert get_mac.json()["value"] == "marimba.aiff"

        # Windows reads back "chord.wav".
        get_win = await _prefs_client.get(
            "/api/v1/user-preferences/notification_sound?platform=windows",
        )
        assert get_win.status_code == 200
        assert get_win.json()["value"] == "chord.wav"

        # The raw rows live under the partitioned keys — verify the
        # list view exposes both partitions so a frontend can
        # introspect the full set.
        listing = await _prefs_client.get("/api/v1/user-preferences")
        assert listing.status_code == 200
        items = listing.json()["items"]
        assert items["notification_sound@platform=macos"] == "marimba.aiff"
        assert items["notification_sound@platform=windows"] == "chord.wav"

    async def test_per_platform_rejects_unsupported_platform(
        self, _prefs_client: AsyncClient,
    ):
        # notification_sound supports macos / windows / linux only.
        resp = await _prefs_client.put(
            "/api/v1/user-preferences/notification_sound?platform=ios",
            json={"value": "ping.caf"},
        )
        assert resp.status_code == 400
        assert "does not support platform" in resp.text

    async def test_never_requires_device_id_param(
        self, _prefs_client: AsyncClient,
    ):
        # hardware_bench_target has sync=never; missing device_id is a 400.
        resp = await _prefs_client.put(
            "/api/v1/user-preferences/hardware_bench_target",
            json={"value": "stm32-board-7"},
        )
        assert resp.status_code == 400
        assert "device_id" in resp.text.lower()

    async def test_never_round_trip_partitions_by_device(
        self, _prefs_client: AsyncClient,
    ):
        put_dev_a = await _prefs_client.put(
            "/api/v1/user-preferences/hardware_bench_target"
            "?device_id=dev-a-123",
            json={"value": "stm32-board-A"},
        )
        assert put_dev_a.status_code == 200

        put_dev_b = await _prefs_client.put(
            "/api/v1/user-preferences/hardware_bench_target"
            "?device_id=dev-b-456",
            json={"value": "rpi5-board-B"},
        )
        assert put_dev_b.status_code == 200

        get_a = await _prefs_client.get(
            "/api/v1/user-preferences/hardware_bench_target"
            "?device_id=dev-a-123",
        )
        assert get_a.json()["value"] == "stm32-board-A"

        get_b = await _prefs_client.get(
            "/api/v1/user-preferences/hardware_bench_target"
            "?device_id=dev-b-456",
        )
        assert get_b.json()["value"] == "rpi5-board-B"

        # Different device id → 404 (no row for that partition).
        get_c = await _prefs_client.get(
            "/api/v1/user-preferences/hardware_bench_target"
            "?device_id=dev-c-789",
        )
        assert get_c.status_code == 404

    async def test_per_platform_auto_sniffs_ua_when_no_param(
        self, _prefs_client: AsyncClient,
    ):
        # No ?platform= → backend sniffs the User-Agent.
        put = await _prefs_client.put(
            "/api/v1/user-preferences/notification_sound",
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64)"},
            json={"value": "chord.wav"},
        )
        assert put.status_code == 200

        # The same UA returns the same value back.
        get = await _prefs_client.get(
            "/api/v1/user-preferences/notification_sound",
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64)"},
        )
        assert get.status_code == 200
        assert get.json()["value"] == "chord.wav"

        # A macOS UA reads from a different partition → 404.
        get_mac = await _prefs_client.get(
            "/api/v1/user-preferences/notification_sound",
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_2)"},
        )
        assert get_mac.status_code == 404

    async def test_globally_synced_setting_unchanged_behaviour(
        self, _prefs_client: AsyncClient,
    ):
        # motion_level is globally synced — query params ignored, bare
        # pref_key row is what the legacy test_user_preferences.py
        # path already locks.
        put = await _prefs_client.put(
            "/api/v1/user-preferences/motion_level?platform=macos",
            json={"value": "normal"},
        )
        assert put.status_code == 200

        # Same value comes back from a different platform — the row is
        # stored under the bare key, not partitioned.
        get_win = await _prefs_client.get(
            "/api/v1/user-preferences/motion_level?platform=windows",
        )
        assert get_win.status_code == 200
        assert get_win.json()["value"] == "normal"


@pytest.mark.asyncio
class TestEmitSuppressionForNever:
    async def test_emit_called_for_globally(self, monkeypatch):
        """Globally synced settings still fire emit_preferences_updated."""
        from backend.routers import preferences as _prefs

        captured: list[tuple] = []

        def _spy(pref_key, value, user_id, *args, **kwargs):
            captured.append((pref_key, value, user_id))

        monkeypatch.setattr(
            "backend.events.emit_preferences_updated", _spy,
        )

        _prefs._emit_preference_updated("motion_level", "subtle", "user-1")
        assert captured == [("motion_level", "subtle", "user-1")]

    async def test_emit_skipped_for_never_base_key(self, monkeypatch):
        """WP.6 — sync=never settings never broadcast."""
        from backend.routers import preferences as _prefs

        captured: list[tuple] = []

        def _spy(pref_key, value, user_id, *args, **kwargs):
            captured.append((pref_key, value, user_id))

        monkeypatch.setattr(
            "backend.events.emit_preferences_updated", _spy,
        )

        # Even the partitioned key (with @device= suffix) should be
        # recognised as belonging to a sync=never base setting.
        _prefs._emit_preference_updated(
            "hardware_bench_target@device=dev-a",
            "stm32-7", "user-1",
        )
        assert captured == []

    async def test_emit_called_for_per_platform(self, monkeypatch):
        """WP.6 — sync=per_platform still broadcasts (different platforms
        get different rows but same-platform sibling devices converge)."""
        from backend.routers import preferences as _prefs

        captured: list[tuple] = []

        def _spy(pref_key, value, user_id, *args, **kwargs):
            captured.append((pref_key, value, user_id))

        monkeypatch.setattr(
            "backend.events.emit_preferences_updated", _spy,
        )

        _prefs._emit_preference_updated(
            "notification_sound@platform=macos",
            "marimba.aiff", "user-1",
        )
        assert len(captured) == 1
        assert captured[0][0] == "notification_sound@platform=macos"
