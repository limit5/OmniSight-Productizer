"""OP-2469 (case5 EPIC-B B2) — WS signaling endpoint.

Routes:

    WS  /webrtc-signaling/rooms/{room_id}/ws?peer_id=<id>

A self-built WebSocket signaling server for the EPIC-B conferencing
WebRTC stack. The brain (:mod:`backend.conf_signaling`) is
auth/transport free; this module is the FastAPI adapter that owns:

    * the WebSocket upgrade and accept,
    * the session/bearer auth shim onto
      :func:`backend.auth.current_user` so the WS path inherits the
      same auth/tenant guard as the REST surface (the ticket calls
      out "reuse the backend auth/tenant guard" — single source of
      truth),
    * the in-band offer/answer/ICE relay with explicit recipient
      targeting (so >2-peer rooms never multicast an SDP),
    * cleanup on disconnect (peer-leave broadcast + registry remove).

Auth — pass and reject
======================

The WebSocket upgrade is a GET handshake; the existing
``backend.auth.current_user`` consumes a :class:`fastapi.Request`.
We adapt by wrapping the :class:`fastapi.WebSocket` in a tiny shim
that exposes ``headers``, ``cookies``, ``client``, ``method`` and
``state``. ``method`` is set to ``POST`` so the GET-fallback branch
in ``current_user`` (which returns the synthetic anonymous user for
read-only requests in ``session`` mode) does NOT apply — signaling
mutates room state, so anonymous joining is rejected outside ``open``
mode.

Pass path:
    * ``OMNISIGHT_AUTH_MODE=open`` → synthetic super-admin (dev/test).
    * Bearer header validates against an API key row.
    * Cookie ``omnisight_session`` resolves to an enabled user.

Reject path (close 1008, in band ``{"type":"error","reason":...}``):
    * No auth in ``session`` / ``strict`` mode.
    * Disabled user.
    * ``peer_id`` query missing, badly shaped, or already taken in
      the room.
    * Room at ``MAX_PEERS_PER_ROOM`` → close 1013.

Tenant guard: room key is ``(user.tenant_id, room_id)``. Two
tenants joining the same room name are isolated by construction —
the relay never crosses tenants because it cannot see across keys.
"""

from __future__ import annotations

import asyncio
import json
import logging
import types
from typing import Any

from fastapi import (
    APIRouter,
    HTTPException,
    Query,
    WebSocket,
    WebSocketDisconnect,
)

from backend import auth as _au
from backend import conf_signaling as _cs

logger = logging.getLogger(__name__)


router = APIRouter(prefix="/webrtc-signaling", tags=["webrtc-signaling"])


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  WebSocket auth shim
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class _WSRequestShim:
    """Duck-typed Request adapter over a :class:`WebSocket`.

    Why not a real :class:`starlette.requests.Request`: the WS scope
    type is ``"websocket"`` not ``"http"`` and constructing an HTTP
    Request from it crosses an assertion in starlette. Exposing the
    handful of attributes ``current_user`` actually reads is the
    minimum invasive adapter; it keeps the auth logic single-sourced
    in :mod:`backend.auth` so a fix there flows through to the WS
    path automatically.
    """

    def __init__(self, ws: WebSocket) -> None:
        self._ws = ws
        # Mutating method — see module docstring. Signaling changes
        # room state, so we deliberately do NOT use ``GET`` here:
        # that would unlock the session-mode read-only fallback in
        # ``current_user`` and let anonymous peers join.
        self.method = "POST"
        self.state = types.SimpleNamespace()

    @property
    def headers(self):
        return self._ws.headers

    @property
    def cookies(self):
        return self._ws.cookies

    @property
    def client(self):
        return self._ws.client


async def verify_ws_user(websocket: WebSocket) -> _au.User:
    """Resolve the authenticated user behind a WS upgrade.

    Reuses :func:`backend.auth.current_user` via :class:`_WSRequestShim`
    so the auth contract (bearer / session cookie / open-mode anon)
    is identical to the REST surface. Raises :class:`HTTPException`
    on auth failure; the caller (the WS endpoint) maps that to a WS
    close.
    """
    shim = _WSRequestShim(websocket)
    return await _au.current_user(shim)  # type: ignore[arg-type]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Helpers — broadcasting + safe enqueue
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def _safe_enqueue(peer: _cs.Peer, envelope: dict[str, Any]) -> bool:
    """Enqueue ``envelope`` for ``peer`` without blocking the relay
    if the peer's queue is full. Returns False on drop — the router
    logs and continues so one stuck peer can't stall the others."""
    try:
        peer.queue.put_nowait(envelope)
        return True
    except asyncio.QueueFull:
        logger.warning(
            "[conf_signaling] dropping %s for peer %s: queue full",
            envelope.get("type"), peer.peer_id,
        )
        return False


async def _broadcast_to_peers(
    peers: list[_cs.Peer], envelope: dict[str, Any], *, exclude: str | None = None,
) -> None:
    """Fan-out an envelope to every peer in ``peers`` except the
    sender. Used for peer-join / peer-leave only; the offer/answer/
    ICE path is targeted (single recipient)."""
    for peer in peers:
        if exclude is not None and peer.peer_id == exclude:
            continue
        await _safe_enqueue(peer, envelope)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  The WebSocket endpoint
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@router.websocket("/rooms/{room_id}/ws")
async def signaling_ws(
    websocket: WebSocket,
    room_id: str,
    peer_id: str = Query(..., min_length=1, max_length=64),
) -> None:
    """Per-peer WebSocket: accepts the upgrade, registers the peer in
    the in-memory room registry, then runs two cooperative loops —
    one reads inbound JSON from the socket and relays to the targeted
    recipient; the other drains the peer's outbound queue and writes
    to the socket. Either loop returning tears down the connection
    and broadcasts ``peer-leave`` to the survivors."""

    # ── Auth (before accept so we close the upgrade with a clean
    # ── 403 if the credentials don't pan out) ────────────────────
    try:
        user = await verify_ws_user(websocket)
    except HTTPException as exc:
        # Closing before accept yields a 403 on the upgrade — the
        # standard FastAPI behaviour. We attach a structured detail
        # so the conf-webrtc-app client can surface the reason.
        await websocket.close(
            code=_cs.WS_CLOSE_POLICY_VIOLATION,
            reason=f"auth: {exc.detail!r}"[:120],
        )
        return
    if not user.enabled:
        await websocket.close(
            code=_cs.WS_CLOSE_POLICY_VIOLATION, reason="user disabled",
        )
        return

    # ── Accept + register peer ─────────────────────────────────────
    await websocket.accept()
    registry = _cs.default_registry()
    tenant_id = user.tenant_id

    try:
        peer, existing_ids = await registry.join(
            tenant_id=tenant_id,
            room_id=room_id,
            peer_id=peer_id,
            user_id=user.id,
        )
    except _cs.PeerIdTakenError:
        await websocket.send_json(_cs.error_envelope(f"peer_id taken: {peer_id}"))
        await websocket.close(
            code=_cs.WS_CLOSE_POLICY_VIOLATION, reason="peer_id taken",
        )
        return
    except _cs.RoomFullError:
        await websocket.send_json(_cs.error_envelope("room full"))
        await websocket.close(
            code=_cs.WS_CLOSE_TRY_AGAIN, reason="room full",
        )
        return

    # ── Welcome + peer-join broadcast ──────────────────────────────
    await websocket.send_json(
        _cs.welcome_envelope(
            room_id=room_id, peer_id=peer_id, peers=existing_ids,
        ),
    )
    others_at_join = [
        p for p in (await registry.peer_ids(tenant_id=tenant_id, room_id=room_id))
        if p != peer_id
    ]
    for other_id in others_at_join:
        other = await registry.get_peer(
            tenant_id=tenant_id, room_id=room_id, peer_id=other_id,
        )
        if other is not None:
            await _safe_enqueue(other, _cs.peer_join_envelope(peer_id=peer_id))

    # ── Reader / writer loops ──────────────────────────────────────
    async def _reader() -> None:
        while True:
            try:
                raw = await websocket.receive_text()
            except WebSocketDisconnect:
                return
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await _safe_enqueue(peer, _cs.error_envelope("invalid JSON"))
                continue
            known = set(await registry.peer_ids(
                tenant_id=tenant_id, room_id=room_id,
            ))
            try:
                target_id, envelope = _cs.validate_signaling_message(
                    msg, sender_peer_id=peer_id, known_peer_ids=known,
                )
            except _cs.SignalingError as err:
                await _safe_enqueue(peer, _cs.error_envelope(str(err)))
                continue
            target = await registry.get_peer(
                tenant_id=tenant_id, room_id=room_id, peer_id=target_id,
            )
            if target is None:
                # Race: target disconnected between known-peer probe
                # and lookup. Treat as soft error so the sender can
                # retry against the latest roster.
                await _safe_enqueue(
                    peer, _cs.error_envelope(f"peer gone: {target_id}"),
                )
                continue
            await _safe_enqueue(target, envelope)

    async def _writer() -> None:
        while True:
            envelope = await peer.queue.get()
            try:
                await websocket.send_json(envelope)
            except WebSocketDisconnect:
                return
            except RuntimeError:
                # ``WebSocket`` raises RuntimeError when the socket has
                # been closed under us by the reader task. Exit cleanly.
                return

    reader_task = asyncio.create_task(_reader())
    writer_task = asyncio.create_task(_writer())
    try:
        done, pending = await asyncio.wait(
            {reader_task, writer_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        for task in done:
            exc = task.exception()
            if exc is not None and not isinstance(exc, WebSocketDisconnect):
                logger.exception(
                    "[conf_signaling] loop crash for peer %s", peer_id,
                    exc_info=exc,
                )
    finally:
        # Cleanup is idempotent — the registry's ``leave`` swallows
        # double-remove safely. We always emit the peer-leave so the
        # remaining peers can tear down their PeerConnections.
        remaining = await registry.leave(
            tenant_id=tenant_id, room_id=room_id, peer_id=peer_id,
        )
        await _broadcast_to_peers(
            remaining, _cs.peer_leave_envelope(peer_id=peer_id),
        )
        try:
            await websocket.close()
        except RuntimeError:
            pass
