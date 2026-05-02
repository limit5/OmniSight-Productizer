"""``backend.hooks`` — git / lifecycle hook entry-points.

Each module under this package is a small, function-only glue layer
between an external trigger (git, systemd, Celery beat, …) and the
real business logic that lives elsewhere in ``backend/``. We keep
hooks thin so the underlying modules stay testable in isolation
without monkey-patching subprocess / argv / git environment.

Currently ships:

* :mod:`backend.hooks.post_merge_docs` — git ``post-merge`` hook
  that calls :mod:`backend.self_healing_docs` after a merge so a
  developer who pulls in new routes locally automatically refreshes
  their working-copy ``openapi.json`` + ``docs/architecture.md``.
"""
