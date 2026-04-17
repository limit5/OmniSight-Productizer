"""R1 (#307) — ChatOps adapters package.

Each adapter (Discord / Teams / Line) implements a tiny surface:

* ``async def send_interactive(channel, message, buttons) -> dict`` — emit an
  outbound interactive message to the adapter's transport (webhook /
  push API).
* ``def parse_inbound(request_headers, raw_body) -> Inbound`` — normalise
  an inbound webhook payload (button click / postback / message) into
  the bridge's canonical :class:`Inbound` shape.
* ``def verify(request_headers, raw_body) -> None`` — raise if the
  request is not cryptographically authentic.

The bridge module re-exports a single ``get_adapter(name)`` factory so
the routers + slash-command layer don't have to know which transport is
wired up at runtime.
"""
