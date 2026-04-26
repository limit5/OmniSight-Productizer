"""omnisight-installer sidecar package.

BS.4 epic — see docs/security/bs-installer-threat-model.md and
docs/design/bs-bootstrap-vertical-aware.md §4.

Package skeleton landed in BS.4.1 (Dockerfile.installer); concrete
modules (main long-poll loop, install methods, progress emitter,
healthz) land in BS.4.2..BS.4.5.
"""
