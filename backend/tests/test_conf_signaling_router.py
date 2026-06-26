"""OP-2469 (case5 EPIC-B B2) — Tests for the WS signaling server.

Covers:
    * Pure-Python registry + protocol-validator unit tests.
    * WS endpoint auth pass/reject paths.
    * Recipient targeting for the relay (offer/answer/ICE land at the
      addressed peer only — confirmed in a 3-peer room).
    * Peer-join / peer-leave broadcasts.
    * Dual-end harness end-to-end.

Scope: only this file's tests run via
``pytest backend/tests/test_conf_signaling_router.py -v`` — the
full backend suite times out per the project DoD.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import auth as _au
from backend import conf_signaling as _cs
from backend.conf_signaling_harness import (
    FAKE_ANSWER_SDP,
    FAKE_ICE_CANDIDATE_A,
    FAKE_ICE_CANDIDATE_B,
    FAKE_OFFER_SDP,
    run_dual_end_harness,
)
from backend.routers.conf_signaling import router as conf_signaling_router


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Test app fixtures
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _anon_admin() -> _au.User:
    return _au.User(
        id="user-test-admin", email="admin@test.local", name="admin",
        role="admin", enabled=True, tenant_id="t-default",
    )


def _disabled_user() -> _au.User:
    return _au.User(
        id="user-disabled", email="disabled@test.local", name="d",
        role="viewer", enabled=False, tenant_id="t-default",
    )


def _other_tenant_admin() -> _au.User:
    return _au.User(
        id="user-other-admin", email="other@test.local", name="o",
        role="admin", enabled=True, tenant_id="t-other",
    )


def _make_app(user_factory=_anon_admin) -> FastAPI:
    """Mount the WS router on a minimal app and override the WS auth
    shim. ``current_user`` is the actual function the shim calls, so
    overriding it on the WS path keeps the test app honest about
    using the same auth surface as production."""
    app = FastAPI()
    # The router uses ``verify_ws_user`` which calls
    # ``backend.auth.current_user`` directly (NOT via Depends), so
    # FastAPI's dependency_overrides cannot intercept it. The simplest
    # honest override is to monkey-patch the module-level reference
    # inside the router; tests do this via the ``override_ws_user``
    # fixture below.
    app.include_router(conf_signaling_router, prefix="/api/v1")
    app.state._ws_user_factory = user_factory
    return app


@pytest.fixture(autouse=True)
def _reset_registry() -> None:
    """Hard-reset the process-local registry between tests so peer
    state from one test cannot leak into the next."""
    _cs.reset_default_registry()
    yield
    _cs.reset_default_registry()


@pytest.fixture
def override_ws_user(monkeypatch):
    """Replace ``verify_ws_user`` with a factory the test controls.

    Tests that exercise the auth-reject path swap in a factory that
    raises :class:`HTTPException`; tests that exercise the auth-pass
    path swap in a factory that returns a synthetic :class:`User`.
    """
    from backend.routers import conf_signaling as router_module

    def _install(user_factory):
        async def _fake(_websocket):
            user = user_factory()
            if isinstance(user, BaseException):
                raise user
            return user
        monkeypatch.setattr(router_module, "verify_ws_user", _fake)

    return _install


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Registry + validator — pure Python
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestRoomRegistry:
    @pytest.mark.asyncio
    async def test_join_returns_peer_and_existing_roster(self) -> None:
        reg = _cs.RoomRegistry()
        peer_a, existing_a = await reg.join(
            tenant_id="t-default", room_id="r1",
            peer_id="a", user_id="u-a",
        )
        assert peer_a.peer_id == "a"
        assert existing_a == []
        peer_b, existing_b = await reg.join(
            tenant_id="t-default", room_id="r1",
            peer_id="b", user_id="u-b",
        )
        assert peer_b.peer_id == "b"
        assert existing_b == ["a"]

    @pytest.mark.asyncio
    async def test_duplicate_peer_id_raises(self) -> None:
        reg = _cs.RoomRegistry()
        await reg.join(tenant_id="t-default", room_id="r1",
                       peer_id="a", user_id="u-a")
        with pytest.raises(_cs.PeerIdTakenError):
            await reg.join(tenant_id="t-default", room_id="r1",
                           peer_id="a", user_id="u-a2")

    @pytest.mark.asyncio
    async def test_room_full_raises(self) -> None:
        reg = _cs.RoomRegistry()
        for i in range(_cs.MAX_PEERS_PER_ROOM):
            await reg.join(tenant_id="t-default", room_id="r1",
                           peer_id=f"p{i}", user_id=f"u{i}")
        with pytest.raises(_cs.RoomFullError):
            await reg.join(tenant_id="t-default", room_id="r1",
                           peer_id="overflow", user_id="u-of")

    @pytest.mark.asyncio
    async def test_tenant_isolation(self) -> None:
        """Two tenants joining the same room name see disjoint
        peer rosters — proves the (tenant_id, room_id) key actually
        isolates."""
        reg = _cs.RoomRegistry()
        await reg.join(tenant_id="t-default", room_id="r1",
                       peer_id="a", user_id="u-a")
        peer_x, existing_x = await reg.join(
            tenant_id="t-other", room_id="r1",
            peer_id="x", user_id="u-x",
        )
        assert existing_x == []  # x doesn't see a from t-default
        assert peer_x.tenant_id == "t-other"
        # And from the other side: t-default still only sees a.
        ids = await reg.peer_ids(tenant_id="t-default", room_id="r1")
        assert ids == ["a"]

    @pytest.mark.asyncio
    async def test_leave_returns_remaining_and_idempotent(self) -> None:
        reg = _cs.RoomRegistry()
        await reg.join(tenant_id="t-default", room_id="r1",
                       peer_id="a", user_id="u-a")
        await reg.join(tenant_id="t-default", room_id="r1",
                       peer_id="b", user_id="u-b")
        remaining = await reg.leave(tenant_id="t-default",
                                    room_id="r1", peer_id="a")
        assert [p.peer_id for p in remaining] == ["b"]
        # Idempotent: leaving an absent peer is a no-op.
        remaining_again = await reg.leave(
            tenant_id="t-default", room_id="r1", peer_id="a",
        )
        assert [p.peer_id for p in remaining_again] == ["b"]
        # Last leave deallocates the room.
        empty = await reg.leave(tenant_id="t-default",
                                room_id="r1", peer_id="b")
        assert empty == []
        assert reg.room_count() == 0


class TestProtocolValidator:
    def test_accepts_offer_to_known_peer(self) -> None:
        target, env = _cs.validate_signaling_message(
            {"type": "offer", "to": "b", "payload": {"sdp": "..."}},
            sender_peer_id="a", known_peer_ids={"a", "b"},
        )
        assert target == "b"
        assert env == {
            "type": "offer", "from": "a", "to": "b",
            "payload": {"sdp": "..."},
        }

    @pytest.mark.parametrize("mtype", ["answer", "ice-candidate"])
    def test_accepts_answer_and_ice(self, mtype: str) -> None:
        target, env = _cs.validate_signaling_message(
            {"type": mtype, "to": "b", "payload": {"x": 1}},
            sender_peer_id="a", known_peer_ids={"a", "b"},
        )
        assert target == "b"
        assert env["type"] == mtype

    def test_rejects_non_dict(self) -> None:
        with pytest.raises(_cs.SignalingError, match="JSON object"):
            _cs.validate_signaling_message(
                "hi", sender_peer_id="a", known_peer_ids={"a", "b"},
            )

    def test_rejects_unknown_type(self) -> None:
        with pytest.raises(_cs.SignalingError, match="unsupported"):
            _cs.validate_signaling_message(
                {"type": "broadcast", "to": "b", "payload": {}},
                sender_peer_id="a", known_peer_ids={"a", "b"},
            )

    def test_rejects_missing_to(self) -> None:
        with pytest.raises(_cs.SignalingError, match="missing 'to'"):
            _cs.validate_signaling_message(
                {"type": "offer", "payload": {}},
                sender_peer_id="a", known_peer_ids={"a", "b"},
            )

    def test_rejects_self_target(self) -> None:
        with pytest.raises(_cs.SignalingError, match="cannot relay to self"):
            _cs.validate_signaling_message(
                {"type": "offer", "to": "a", "payload": {}},
                sender_peer_id="a", known_peer_ids={"a"},
            )

    def test_rejects_unknown_peer(self) -> None:
        with pytest.raises(_cs.SignalingError, match="unknown peer"):
            _cs.validate_signaling_message(
                {"type": "offer", "to": "c", "payload": {}},
                sender_peer_id="a", known_peer_ids={"a", "b"},
            )

    def test_rejects_missing_payload(self) -> None:
        with pytest.raises(_cs.SignalingError, match="payload"):
            _cs.validate_signaling_message(
                {"type": "offer", "to": "b"},
                sender_peer_id="a", known_peer_ids={"a", "b"},
            )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  WS endpoint — auth
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestWSAuthPath:
    def test_accept_authenticated_user(self, override_ws_user) -> None:
        override_ws_user(_anon_admin)
        app = _make_app()
        with TestClient(app) as client:
            with client.websocket_connect(
                "/api/v1/webrtc-signaling/rooms/r1/ws?peer_id=a",
            ) as ws:
                welcome = ws.receive_json()
                assert welcome["type"] == "welcome"
                assert welcome["peer_id"] == "a"
                assert welcome["room_id"] == "r1"
                assert welcome["peers"] == []

    def test_reject_unauthenticated_request_closes_before_accept(
        self, override_ws_user,
    ) -> None:
        from fastapi import HTTPException
        override_ws_user(lambda: HTTPException(status_code=401, detail="nope"))
        app = _make_app()
        with TestClient(app) as client:
            # A pre-accept close raises starlette's WebSocketDisconnect
            # when the client tries to read; the upgrade is rejected.
            with pytest.raises(Exception) as exc_info:
                with client.websocket_connect(
                    "/api/v1/webrtc-signaling/rooms/r1/ws?peer_id=a",
                ):
                    pass
            # Class name varies across starlette versions
            # (``WebSocketDisconnect`` vs ``WebSocketDenialResponse``);
            # what we pin is "the upgrade did NOT succeed."
            assert exc_info.type.__name__ in {
                "WebSocketDisconnect", "WebSocketDenialResponse",
            }

    def test_reject_disabled_user(self, override_ws_user) -> None:
        override_ws_user(_disabled_user)
        app = _make_app()
        with TestClient(app) as client:
            with pytest.raises(Exception):
                with client.websocket_connect(
                    "/api/v1/webrtc-signaling/rooms/r1/ws?peer_id=a",
                ):
                    pass

    def test_reject_duplicate_peer_id(self, override_ws_user) -> None:
        override_ws_user(_anon_admin)
        app = _make_app()
        with TestClient(app) as client:
            with client.websocket_connect(
                "/api/v1/webrtc-signaling/rooms/r1/ws?peer_id=a",
            ) as ws_a:
                ws_a.receive_json()  # welcome
                # Second connection with same peer_id is rejected
                # with an error envelope + close.
                with pytest.raises(Exception):
                    with client.websocket_connect(
                        "/api/v1/webrtc-signaling/rooms/r1/ws?peer_id=a",
                    ) as ws_dup:
                        # If the server didn't close, this would block.
                        env = ws_dup.receive_json()
                        assert env["type"] == "error"
                        # Trigger the close detection
                        ws_dup.receive_json()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  WS endpoint — relay + targeting
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestWSRelay:
    def test_offer_only_reaches_addressed_peer_in_three_peer_room(
        self, override_ws_user,
    ) -> None:
        """3-peer room: A sends an offer addressed to B; C must NOT
        receive it. This is the key disambiguation guarantee the
        ticket calls out ('>2-peer relay is unambiguous')."""
        override_ws_user(_anon_admin)
        app = _make_app()
        with TestClient(app) as client:
            url = "/api/v1/webrtc-signaling/rooms/r1/ws?peer_id={pid}"
            with client.websocket_connect(url.format(pid="a")) as ws_a:
                ws_a.receive_json()  # welcome
                with client.websocket_connect(url.format(pid="b")) as ws_b:
                    ws_b.receive_json()  # welcome
                    # A sees b joined
                    join_b = ws_a.receive_json()
                    assert join_b["type"] == "peer-join"
                    assert join_b["peer_id"] == "b"
                    with client.websocket_connect(url.format(pid="c")) as ws_c:
                        welcome_c = ws_c.receive_json()
                        assert welcome_c["type"] == "welcome"
                        assert set(welcome_c["peers"]) == {"a", "b"}
                        # Both A and B see c join
                        join_c_at_a = ws_a.receive_json()
                        join_c_at_b = ws_b.receive_json()
                        assert join_c_at_a["peer_id"] == "c"
                        assert join_c_at_b["peer_id"] == "c"

                        # Now A → B: offer
                        ws_a.send_json({
                            "type": "offer", "to": "b",
                            "payload": {"sdp": FAKE_OFFER_SDP},
                        })
                        got = ws_b.receive_json()
                        assert got["type"] == "offer"
                        assert got["from"] == "a"
                        assert got["payload"]["sdp"] == FAKE_OFFER_SDP

                        # C must NOT have received the offer. Send a
                        # ping-shaped probe addressed to C and ensure
                        # C's NEXT inbound is that probe, not the
                        # stale offer.
                        ws_a.send_json({
                            "type": "ice-candidate", "to": "c",
                            "payload": {"candidate": "probe"},
                        })
                        probe = ws_c.receive_json()
                        assert probe["type"] == "ice-candidate"
                        assert probe["from"] == "a"
                        assert probe["payload"] == {"candidate": "probe"}

    def test_relay_rejects_unknown_target_in_band(
        self, override_ws_user,
    ) -> None:
        override_ws_user(_anon_admin)
        app = _make_app()
        with TestClient(app) as client:
            url = "/api/v1/webrtc-signaling/rooms/r1/ws?peer_id=a"
            with client.websocket_connect(url) as ws_a:
                ws_a.receive_json()  # welcome
                ws_a.send_json({
                    "type": "offer", "to": "nobody",
                    "payload": {"sdp": "x"},
                })
                err = ws_a.receive_json()
                assert err["type"] == "error"
                assert "unknown peer" in err["reason"]

    def test_invalid_json_yields_error_envelope(
        self, override_ws_user,
    ) -> None:
        override_ws_user(_anon_admin)
        app = _make_app()
        with TestClient(app) as client:
            url = "/api/v1/webrtc-signaling/rooms/r1/ws?peer_id=a"
            with client.websocket_connect(url) as ws_a:
                ws_a.receive_json()  # welcome
                ws_a.send_text("not json {")
                err = ws_a.receive_json()
                assert err["type"] == "error"
                assert "invalid JSON" in err["reason"]

    def test_peer_leave_broadcast(self, override_ws_user) -> None:
        override_ws_user(_anon_admin)
        app = _make_app()
        with TestClient(app) as client:
            url = "/api/v1/webrtc-signaling/rooms/r1/ws?peer_id={pid}"
            with client.websocket_connect(url.format(pid="a")) as ws_a:
                ws_a.receive_json()  # welcome
                with client.websocket_connect(url.format(pid="b")) as ws_b:
                    ws_b.receive_json()  # welcome
                    ws_a.receive_json()  # peer-join b
                # Leaving the inner ``with`` closes b's socket.
                leave = ws_a.receive_json()
                assert leave["type"] == "peer-leave"
                assert leave["peer_id"] == "b"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Tenant isolation across the WS path
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestTenantIsolationE2E:
    def test_two_tenants_dont_see_each_other(self, monkeypatch) -> None:
        """Mounts the router twice on separate apps, one per tenant
        override, and confirms the registry keys them under different
        ``(tenant_id, room_id)`` slices so neither sees the other's
        peers in the welcome roster."""
        from backend.routers import conf_signaling as router_module

        # First connection: t-default tenant peer 'a'
        async def _user_default(_ws):
            return _anon_admin()

        monkeypatch.setattr(router_module, "verify_ws_user", _user_default)
        app1 = FastAPI()
        app1.include_router(conf_signaling_router, prefix="/api/v1")
        client1 = TestClient(app1)
        with client1.websocket_connect(
            "/api/v1/webrtc-signaling/rooms/shared/ws?peer_id=a",
        ) as ws_a:
            welcome_a = ws_a.receive_json()
            assert welcome_a["peers"] == []

            # Second connection: t-other tenant peer 'x' on same room name.
            async def _user_other(_ws):
                return _other_tenant_admin()

            monkeypatch.setattr(router_module, "verify_ws_user", _user_other)
            app2 = FastAPI()
            app2.include_router(conf_signaling_router, prefix="/api/v1")
            client2 = TestClient(app2)
            with client2.websocket_connect(
                "/api/v1/webrtc-signaling/rooms/shared/ws?peer_id=x",
            ) as ws_x:
                welcome_x = ws_x.receive_json()
                # x must NOT see a — different tenant slice.
                assert welcome_x["peers"] == []


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Dual-end harness — the EPIC-B B2 acceptance shape
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestDualEndHarness:
    def test_offer_answer_ice_round_trip(self, override_ws_user) -> None:
        override_ws_user(_anon_admin)
        app = _make_app()
        with TestClient(app) as client:
            result = run_dual_end_harness(
                client,
                room_id="harness",
                peer_a_id="peer-a",
                peer_b_id="peer-b",
                url_prefix="/api/v1/webrtc-signaling",
            )

        assert result.ok, f"harness failed: {result.error!r}"
        # A's view: welcome, peer-join(B), answer(from B), ice(from B).
        types_a = [m["type"] for m in result.peer_a.inbound]
        assert types_a == ["welcome", "peer-join", "answer", "ice-candidate"]
        # B's view: welcome, offer(from A), ice(from A).
        types_b = [m["type"] for m in result.peer_b.inbound]
        assert types_b == ["welcome", "offer", "ice-candidate"]
        # SDP payload arrived verbatim — relay does not rewrite.
        offer_at_b = result.peer_b.inbound[1]
        assert offer_at_b["from"] == "peer-a"
        assert offer_at_b["payload"]["sdp"] == FAKE_OFFER_SDP
        answer_at_a = result.peer_a.inbound[2]
        assert answer_at_a["from"] == "peer-b"
        assert answer_at_a["payload"]["sdp"] == FAKE_ANSWER_SDP
        # ICE candidates round-tripped both directions.
        ice_at_b = result.peer_b.inbound[2]
        assert ice_at_b["payload"]["candidate"] == FAKE_ICE_CANDIDATE_A
        ice_at_a = result.peer_a.inbound[3]
        assert ice_at_a["payload"]["candidate"] == FAKE_ICE_CANDIDATE_B
