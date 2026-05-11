"""OP-904 (F6) — Cross-task awareness API package.

Houses HTTP surfaces that aggregate signal from multiple Sprint-C/F
subsystems (JIRA, Cognee, Graphiti, failure-graph) under a single
versioned route. Mounted from :mod:`backend.main` via
``_include_versioned_router`` so the canonical path is
``/api/v1/project-state``.
"""
