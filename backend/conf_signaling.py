"""OP-2469 (case5 EPIC-B B2) — Self-built WebSocket signaling core.

Process-local in-memory room registry plus pure-Python protocol
helpers used by the WS router (:mod:`backend.routers.conf_signaling`)
and the dual-end harness (:mod:`backend.conf_signaling_harness`).

Why "self-built": EPIC-B C5 conferencing leaves the SDP offer/answer
and ICE relay to backend code that the team owns, rather than a
vendor signaling SaaS, so the same auth/tenant guard that protects
the rest of the Productizer backend protects the WebRTC handshake.
This module is the auth-free, transport-free brain — the router
adapts FastAPI WebSockets to it; the harness adapts test peers.

Multi-worker note
-----------------
Single uvicorn worker assumption: signaling state lives in
process memory and is NOT shared between workers. The Productizer
backend runs a single worker for the conferencing surface (the
device-side conf-webrtc-app maintains its own session affinity).
Cross-worker fan-out (Redis pub/sub) is a deferred follow-up; for
the B2 self-built + B1 conf-webrtc-app dual-end gate the single
worker assumption holds. ``RoomRegistry`` is therefore an
``asyncio.Lock``-guarded dict, not a distributed structure.

Protocol — JSON, recipient-targeted
-----------------------------------
Three relayable message types: ``offer``, ``answer``,
``ice-candidate``. Every client → server envelope MUST carry an
explicit ``to`` peer id so a 3+ peer room never multicasts an SDP
to a peer that didn't ask for it (this disambiguates the >2-peer
relay path called out by the ticket).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Protocol constants
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

MSG_OFFER = "offer"
MSG_ANSWER = "answer"
MSG_ICE = "ice-candidate"

MSG_WELCOME = "welcome"
MSG_PEER_JOIN = "peer-join"
MSG_PEER_LEAVE = "peer-leave"
MSG_ERROR = "error"

# Client → server message types the router will relay. Anything else
# is rejected at validate time (the router also rejects malformed
# JSON before we get here).
_RELAYABLE: frozenset[str] = frozenset({MSG_OFFER, MSG_ANSWER, MSG_ICE})


# Bounded resources — keep memory + queue depth predictable even if
# a misbehaving client tries to flood. Both limits are deliberately
# generous for a real conference (3-8 peers, low message rate) but
# stop a runaway loop in tests / fuzz.
MAX_PEERS_PER_ROOM = 16
MAX_QUEUE_SIZE = 256

# WS close codes used by the router. 1008 = policy violation
# (auth / tenant / protocol); 1013 = try again (room full).
WS_CLOSE_POLICY_VIOLATION = 1008
WS_CLOSE_TRY_AGAIN = 1013


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Errors
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class SignalingError(Exception):
    """Base class for room-registry + protocol violations."""


class PeerIdTakenError(SignalingError):
    """Raised when a peer tries to join a room with a peer_id that is
    already in use. The router maps this to WS 1008."""


class RoomFullError(SignalingError):
    """Raised when ``MAX_PEERS_PER_ROOM`` is already reached. The
    router maps this to WS 1013 (try again later)."""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Data classes
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class Peer:
    peer_id: str
    user_id: str
    tenant_id: str
    queue: asyncio.Queue
    joined_at: float


@dataclass
class Room:
    tenant_id: str
    room_id: str
    peers: dict[str, Peer] = field(default_factory=dict)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Registry
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class RoomRegistry:
    """In-memory registry of conferencing rooms, keyed by
    ``(tenant_id, room_id)``.

    Tenant id is part of the key so two tenants who pick the same
    nominal room name (``standup``, ``demo``) are isolated — the
    relay logic only ever sees peers from the same tenant slice.
    """

    def __init__(self) -> None:
        self._rooms: dict[tuple[str, str], Room] = {}
        self._lock = asyncio.Lock()

    async def join(
        self,
        *,
        tenant_id: str,
        room_id: str,
        peer_id: str,
        user_id: str,
    ) -> tuple[Peer, list[str]]:
        """Register ``peer_id`` in the ``(tenant_id, room_id)`` room.

        Returns the newly-allocated :class:`Peer` plus the ids of the
        peers already present so the router can emit a ``welcome``
        with the existing roster.

        Raises:
            PeerIdTakenError: when ``peer_id`` already exists in room.
            RoomFullError: when the room is at ``MAX_PEERS_PER_ROOM``.
        """
        async with self._lock:
            key = (tenant_id, room_id)
            room = self._rooms.get(key)
            if room is None:
                room = Room(tenant_id=tenant_id, room_id=room_id)
                self._rooms[key] = room
            if peer_id in room.peers:
                raise PeerIdTakenError(peer_id)
            if len(room.peers) >= MAX_PEERS_PER_ROOM:
                raise RoomFullError(room_id)
            peer = Peer(
                peer_id=peer_id,
                user_id=user_id,
                tenant_id=tenant_id,
                queue=asyncio.Queue(maxsize=MAX_QUEUE_SIZE),
                joined_at=time.time(),
            )
            existing = list(room.peers.keys())
            room.peers[peer_id] = peer
            return peer, existing

    async def leave(
        self, *, tenant_id: str, room_id: str, peer_id: str,
    ) -> list[Peer]:
        """Remove ``peer_id`` and return remaining peers (for the
        leave broadcast). Idempotent — leaving an absent peer is a
        no-op so duplicate cleanup paths (router finally + harness
        abort) are safe."""
        async with self._lock:
            key = (tenant_id, room_id)
            room = self._rooms.get(key)
            if room is None:
                return []
            room.peers.pop(peer_id, None)
            if not room.peers:
                self._rooms.pop(key, None)
                return []
            return list(room.peers.values())

    async def get_peer(
        self, *, tenant_id: str, room_id: str, peer_id: str,
    ) -> Peer | None:
        async with self._lock:
            room = self._rooms.get((tenant_id, room_id))
            return None if room is None else room.peers.get(peer_id)

    async def peer_ids(
        self, *, tenant_id: str, room_id: str,
    ) -> list[str]:
        async with self._lock:
            room = self._rooms.get((tenant_id, room_id))
            return [] if room is None else list(room.peers.keys())

    def room_count(self) -> int:
        return len(self._rooms)


_default_registry: RoomRegistry | None = None


def default_registry() -> RoomRegistry:
    """Process-local singleton used by the router by default. Tests
    that want isolation should call :func:`reset_default_registry`
    in setup."""
    global _default_registry
    if _default_registry is None:
        _default_registry = RoomRegistry()
    return _default_registry


def reset_default_registry() -> None:
    """Test hook — replace the process-local registry with a fresh
    one so cross-test peer state cannot leak."""
    global _default_registry
    _default_registry = RoomRegistry()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Protocol helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def validate_signaling_message(
    msg: Any,
    *,
    sender_peer_id: str,
    known_peer_ids: set[str],
) -> tuple[str, dict[str, Any]]:
    """Validate an inbound client message and return
    ``(target_peer_id, envelope)`` where ``envelope`` is the JSON
    object the router will deliver to the target peer.

    The envelope stamps ``from`` so the receiver always knows who
    sent the SDP/candidate — the client can't forge it.

    Raises:
        SignalingError: on any protocol violation. The router maps
        these to an in-band ``{"type":"error",...}`` reply rather
        than tearing down the socket, so a single fat-fingered
        client message can't kick its own session.
    """
    if not isinstance(msg, dict):
        raise SignalingError("message must be a JSON object")
    mtype = msg.get("type")
    if mtype not in _RELAYABLE:
        raise SignalingError(f"unsupported message type: {mtype!r}")
    to = msg.get("to")
    if not isinstance(to, str) or not to:
        raise SignalingError("message missing 'to' peer_id")
    if to == sender_peer_id:
        raise SignalingError("cannot relay to self")
    if to not in known_peer_ids:
        raise SignalingError(f"unknown peer: {to}")
    payload = msg.get("payload")
    if not isinstance(payload, (dict, list, str)):
        raise SignalingError("message missing 'payload'")
    envelope: dict[str, Any] = {
        "type": mtype,
        "from": sender_peer_id,
        "to": to,
        "payload": payload,
    }
    return to, envelope


def welcome_envelope(*, room_id: str, peer_id: str, peers: list[str]) -> dict[str, Any]:
    """Server → client message the router sends right after accepting
    the WS upgrade so the new peer learns who is already in the room."""
    return {
        "type": MSG_WELCOME,
        "room_id": room_id,
        "peer_id": peer_id,
        "peers": peers,
    }


def peer_join_envelope(*, peer_id: str) -> dict[str, Any]:
    """Server → existing peers broadcast when a new peer joins."""
    return {"type": MSG_PEER_JOIN, "peer_id": peer_id}


def peer_leave_envelope(*, peer_id: str) -> dict[str, Any]:
    """Server → remaining peers broadcast when a peer disconnects."""
    return {"type": MSG_PEER_LEAVE, "peer_id": peer_id}


def error_envelope(reason: str) -> dict[str, Any]:
    """Server → sender in-band error reply. The router keeps the
    socket open and lets the client recover (e.g. resend with a
    valid ``to``)."""
    return {"type": MSG_ERROR, "reason": reason}
