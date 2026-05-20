"""Shared release-cut Gerrit metadata constants."""

CANONICAL_RELEASE_CUT_HASHTAG = "R3-fastforward"
RELEASE_CUT_HASHTAGS: tuple[str, ...] = (
    "auto-promote",
    CANONICAL_RELEASE_CUT_HASHTAG,
)
RELEASE_CUT_PROMOTE_REQUIREMENT = "release-cut-promote"

__all__ = [
    "CANONICAL_RELEASE_CUT_HASHTAG",
    "RELEASE_CUT_HASHTAGS",
    "RELEASE_CUT_PROMOTE_REQUIREMENT",
]
