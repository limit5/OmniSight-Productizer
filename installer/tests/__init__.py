"""BS.4.7 — installer sidecar test package.

Tests live under ``installer/tests/`` (not ``backend/tests/``) so the
sidecar's contract is verified independently of backend pytest config.
Run them with::

    pytest installer/tests/

The conftest in this directory pins ``sys.path`` to the repo root so
``installer.*`` imports resolve regardless of the cwd pytest was
invoked from.
"""
