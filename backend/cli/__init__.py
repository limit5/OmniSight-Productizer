"""V9 #2 / #325 — ``omnisight`` CLI MVP.

Terminal equivalent of the workspace dashboard + R1 ChatOps surface:

* ``omnisight status``              — system KPI snapshot
* ``omnisight workspace list``      — active workspace table
* ``omnisight run "NL prompt"``     — drive ``POST /invoke/stream``
* ``omnisight inspect <agent_id>``  — agent detail + workspace pointer
* ``omnisight inject <agent_id> "hint"`` — operator hint via ChatOps

The top-level Click group is re-exported as ``cli`` so entry-point
plumbing can ``python -m backend.cli`` or ``from backend.cli import cli``.
"""

from backend.cli.main import cli

__all__ = ["cli"]
