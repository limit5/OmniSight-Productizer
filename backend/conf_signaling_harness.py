"""OP-2469 (case5 EPIC-B B2) — Dual-end harness for the WS signaling
server.

A self-contained driver that connects TWO peers (A and B) to the
WebSocket signaling endpoint via :class:`fastapi.testclient.TestClient`
and runs the full offer / answer / ICE exchange end-to-end. Lives in
``backend/`` (not under ``tests/``) so it can be imported from:

    * the EPIC-B B2 unit tests (this is where it's exercised today);
    * a future B1 conf-webrtc-app integration smoke (the "real
      conf-webrtc-app connects" AC-Exercised gate).

The harness is dependency-free at runtime — it only needs a FastAPI
``app`` instance that has the signaling router mounted plus a way to
authenticate. Tests pass ``OMNISIGHT_AUTH_MODE=open`` (the synthetic
anonymous super-admin), so no real session/bearer is needed.

The contract returned by :func:`run_dual_end_harness` is a typed
result with the messages each peer observed; assertions in tests
pattern-match on it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# The harness uses ASCII-only fake SDP / ICE blobs. The signaling
# server does not parse these — they are opaque to the relay — so a
# string is enough to assert "the right bytes landed at the right
# peer." Keeps the harness free of any aiortc / wrtc dependency.
FAKE_OFFER_SDP = "v=0\no=- 1 1 IN IP4 0.0.0.0\ns=harness\nt=0 0\nm=video"
FAKE_ANSWER_SDP = "v=0\no=- 2 2 IN IP4 0.0.0.0\ns=harness\nt=0 0\nm=video"
FAKE_ICE_CANDIDATE_A = (
    "candidate:1 1 UDP 2122252543 192.0.2.10 50000 typ host"
)
FAKE_ICE_CANDIDATE_B = (
    "candidate:1 1 UDP 2122252543 192.0.2.20 60000 typ host"
)


@dataclass
class HarnessPeerLog:
    """Everything one harness peer observed during the run."""

    welcome: dict[str, Any] | None = None
    inbound: list[dict[str, Any]] = field(default_factory=list)
    sent: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class HarnessResult:
    """Aggregated outcome of one dual-end harness run."""

    room_id: str
    peer_a: HarnessPeerLog
    peer_b: HarnessPeerLog
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def _read_until(ws: Any, predicate, *, max_frames: int = 32) -> dict[str, Any]:
    """Drain frames from ``ws`` until ``predicate(envelope)`` returns
    True. Skips frames that don't match (keeps the harness robust
    against future server-side keep-alives that might land first).
    Caller's pytest timeout bounds total wall-clock; we bound frame
    count to surface infinite-recv regressions in CI."""
    for _ in range(max_frames):
        env = ws.receive_json()
        if predicate(env):
            return env
    raise RuntimeError(
        f"harness drained {max_frames} frames without matching predicate",
    )


def run_dual_end_harness(
    client: Any,
    *,
    room_id: str = "harness-room",
    peer_a_id: str = "peer-a",
    peer_b_id: str = "peer-b",
    url_prefix: str = "/api/v1/webrtc-signaling",
) -> HarnessResult:
    """Run the dual-end conferencing flow A→B→A and return a
    structured :class:`HarnessResult`.

    Sequence (this is the contract pinned by the unit tests and the
    one the B1 conf-webrtc-app integration will follow):

        1. A connects → reads welcome.
        2. B connects → reads welcome (sees A in roster).
        3. A reads peer-join for B.
        4. A sends ``offer`` targeting B.
        5. B reads the offer.
        6. B sends ``answer`` targeting A.
        7. A reads the answer.
        8. A sends an ICE candidate targeting B; B reads it.
        9. B sends an ICE candidate targeting A; A reads it.

    Any failed step short-circuits and is recorded in
    :attr:`HarnessResult.error`.
    """

    log_a = HarnessPeerLog()
    log_b = HarnessPeerLog()
    result = HarnessResult(room_id=room_id, peer_a=log_a, peer_b=log_b)

    a_url = f"{url_prefix}/rooms/{room_id}/ws?peer_id={peer_a_id}"
    b_url = f"{url_prefix}/rooms/{room_id}/ws?peer_id={peer_b_id}"

    try:
        with client.websocket_connect(a_url) as ws_a:
            log_a.welcome = _read_until(
                ws_a, lambda e: e.get("type") == "welcome",
            )
            log_a.inbound.append(log_a.welcome)

            with client.websocket_connect(b_url) as ws_b:
                log_b.welcome = _read_until(
                    ws_b, lambda e: e.get("type") == "welcome",
                )
                log_b.inbound.append(log_b.welcome)

                # A should see a peer-join for B.
                join_b = _read_until(
                    ws_a, lambda e: e.get("type") == "peer-join",
                )
                log_a.inbound.append(join_b)

                # 4) A → B: offer
                offer_msg = {
                    "type": "offer",
                    "to": peer_b_id,
                    "payload": {"type": "offer", "sdp": FAKE_OFFER_SDP},
                }
                ws_a.send_json(offer_msg)
                log_a.sent.append(offer_msg)

                # 5) B receives the offer
                got_offer = _read_until(
                    ws_b, lambda e: e.get("type") == "offer",
                )
                log_b.inbound.append(got_offer)

                # 6) B → A: answer
                answer_msg = {
                    "type": "answer",
                    "to": peer_a_id,
                    "payload": {"type": "answer", "sdp": FAKE_ANSWER_SDP},
                }
                ws_b.send_json(answer_msg)
                log_b.sent.append(answer_msg)

                # 7) A receives the answer
                got_answer = _read_until(
                    ws_a, lambda e: e.get("type") == "answer",
                )
                log_a.inbound.append(got_answer)

                # 8) A → B: ICE
                ice_a = {
                    "type": "ice-candidate",
                    "to": peer_b_id,
                    "payload": {"candidate": FAKE_ICE_CANDIDATE_A},
                }
                ws_a.send_json(ice_a)
                log_a.sent.append(ice_a)
                got_ice_a = _read_until(
                    ws_b, lambda e: e.get("type") == "ice-candidate",
                )
                log_b.inbound.append(got_ice_a)

                # 9) B → A: ICE
                ice_b = {
                    "type": "ice-candidate",
                    "to": peer_a_id,
                    "payload": {"candidate": FAKE_ICE_CANDIDATE_B},
                }
                ws_b.send_json(ice_b)
                log_b.sent.append(ice_b)
                got_ice_b = _read_until(
                    ws_a, lambda e: e.get("type") == "ice-candidate",
                )
                log_a.inbound.append(got_ice_b)

    except Exception as exc:  # noqa: BLE001 — record + surface to caller
        result.error = f"{type(exc).__name__}: {exc}"

    return result
