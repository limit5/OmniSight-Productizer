"""Regression guards for the talent-options endpoint's config dependency.

Two failure modes are covered:

1. **Packaging** — ``config/talent_tree.yaml`` (the SINGULAR ``config/`` dir)
   must exist at ``TALENT_TREE_PATH`` and be loadable. The v0.7.12 prod 500 was
   caused by ``Dockerfile.backend`` copying ``configs/`` (plural) but not
   ``config/`` (singular), so the file was absent in the image.
2. **Error mapping** — if the tree YAML is unreadable at request time, the GET
   endpoint must surface a deterministic 503 (deployment fault), never an
   opaque 500 (uncaught ``FileNotFoundError``).
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

import backend.routers.agents as agents_router
from backend.agents.talent_tree import TALENT_TREE_PATH, load_talent_tree


def test_talent_tree_config_is_present_and_loadable() -> None:
    """The tree the endpoint reads must be packaged and parse cleanly."""
    assert TALENT_TREE_PATH.is_file(), (
        f"{TALENT_TREE_PATH} missing — ensure Dockerfile.backend copies the "
        "singular config/ dir (distinct from configs/)"
    )
    tree = load_talent_tree()
    assert tree, "talent tree loaded empty"


def test_options_endpoint_maps_missing_config_to_503(monkeypatch) -> None:
    """A FileNotFoundError from the tree loader → 503, not a bare 500."""

    def _boom(*_args, **_kwargs):
        raise FileNotFoundError("No such file or directory: config/talent_tree.yaml")

    monkeypatch.setattr(agents_router, "available_talents", _boom)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            agents_router.get_agent_talent_options(
                agent_id="iris", guild="isp", milestone=10, conn=None
            )
        )
    assert exc_info.value.status_code == 503
    assert "talent_tree.yaml" in str(exc_info.value.detail)
