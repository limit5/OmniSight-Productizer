"""Y6 #282 row 7 — Same-name repo collision regression tests.

Locks the contract introduced by the seventh sub-bullet under Y6 in TODO.md::

    同名 repo collision：clone 前算 ``url_hash``，不同 url 走不同 sub-dir；
    避免 repo-name 相同時互相覆蓋（audit 找到的 bug）。

The collision-prevention mechanism itself — ``sha256(remote_url)[:16]`` as
the leaf sub-dir under ``{tid}/{pl}/{pid}/{agent_id}/`` — was implemented
as part of row 1 and is exercised by ``test_workspace_hierarchy.py``.
This file is the **dedicated audit-bug regression test** for row 7:

* Hash is computed BEFORE the clone/worktree command (so the collision
  is averted at path-resolution time, not after a partial clone has
  scribbled to the wrong dir).
* Three or more repos sharing the SAME basename but DIFFERENT URLs
  must coexist on disk under three distinct hash leaves — the audit
  bug originally reported a 2-URL silent overwrite; we extend coverage
  to 3+ URLs to catch any "first-N-only" off-by-one regressions.
* The pre-clone log line emits the (agent_id, source, url_hash, leaf)
  tuple so operators can forensically prove which URL produced which
  on-disk dir.
* The SSE ``workspace.provisioned`` event detail string carries the
  ``url_hash=`` token so dashboards subscribed to the bus see the
  collision-safety leaf without scraping the full path.

These tests are pure-unit — they bring up real local git repos under
``tmp_path`` rather than stubbing git, so the collision-safety property
is validated end-to-end against the live ``provision()`` code path.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import subprocess
from pathlib import Path

import pytest

from backend import workspace as ws_mod


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _git(*args: str, cwd: Path) -> str:
    return subprocess.check_output(
        ["git", *args], cwd=str(cwd), text=True,
        stderr=subprocess.STDOUT,
    ).strip()


def _make_repo(path: Path, marker: str) -> Path:
    """Create a tiny initialised git repo with one tracked file. ``marker``
    becomes the file content so collision tests can prove the *correct*
    repo's content survived."""
    path.mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=path)
    _git("config", "user.email", "test@local", cwd=path)
    _git("config", "user.name", "test", cwd=path)
    (path / "README.md").write_text(marker)
    _git("add", "README.md", cwd=path)
    _git("commit", "-q", "-m", "initial", cwd=path)
    return path


@pytest.fixture
def redirected_ws_root(tmp_path: Path, monkeypatch):
    root = tmp_path / "ws_root"
    root.mkdir()
    monkeypatch.setattr(ws_mod, "_WORKSPACES_ROOT", root, raising=True)
    return root


@pytest.fixture(autouse=True)
def empty_registry(monkeypatch):
    """Each test starts with an empty in-process workspace registry — keeps
    cross-test isolation tight without us having to remember to clean up."""
    monkeypatch.setattr(ws_mod, "_workspaces", {}, raising=True)


@pytest.fixture
def captured_sse(monkeypatch):
    """Replace ``backend.events.bus.publish`` with a recorder so we can
    assert the SSE ``workspace.provisioned`` event detail carries the
    ``url_hash=`` token (row 7 collision-safety contract)."""
    from backend import events as _events
    captured: list[tuple[str, dict]] = []

    def _publish(topic, payload, *args, **kwargs):
        captured.append((topic, payload))

    monkeypatch.setattr(_events.bus, "publish", _publish, raising=True)
    return captured


# ---------------------------------------------------------------------------
# 1) Pre-clone hash log — operators can forensically prove which URL
#    produced which on-disk leaf BEFORE the clone command runs
# ---------------------------------------------------------------------------


def test_pre_clone_url_hash_log_emitted(
    tmp_path: Path, redirected_ws_root, caplog,
):
    """``provision()`` must log the (agent_id, source, url_hash, leaf)
    tuple BEFORE the clone/worktree command runs — that log line is the
    operator-visible evidence the collision-safety leaf was assigned at
    path-resolution time, not after a partial clone scribbled to the
    wrong dir.
    """
    repo = _make_repo(tmp_path / "src" / "audit-collision-repo", marker="A\n")
    expected_hash = ws_mod._repo_url_hash(str(repo))

    with caplog.at_level(logging.INFO, logger="backend.workspace"):
        info = asyncio.run(ws_mod.provision(
            agent_id="row7-prelog-agent",
            task_id="row7-task",
            repo_source=str(repo),
        ))
    try:
        # The Y6 row 7 line is tagged so a future grep can isolate it
        # from the dozens of other "Workspace ..." logs.
        url_hash_lines = [
            r for r in caplog.records
            if "Workspace url_hash assigned" in r.getMessage()
        ]
        assert url_hash_lines, (
            "expected a pre-clone url_hash log line tagged "
            "'Workspace url_hash assigned' — none found"
        )
        msg = url_hash_lines[-1].getMessage()
        # All four pieces of forensic evidence must be present.
        assert "row7-prelog-agent" in msg
        assert str(repo) in msg
        assert f"url_hash={expected_hash}" in msg
        assert f"leaf={info.path.name}" in msg
        # And the leaf in the log must equal the actual hash value
        # (i.e. we didn't pre-compute one and clone to a different one).
        assert info.path.name == expected_hash
    finally:
        asyncio.run(ws_mod.cleanup("row7-prelog-agent"))


def test_pre_clone_log_uses_self_repo_marker_for_in_repo(
    redirected_ws_root, caplog,
):
    """For in-repo worktrees the pre-clone log surfaces ``<self-repo>``
    as the source token (not the full ``_MAIN_REPO`` path) so an
    operator immediately recognises the in-repo case versus an external
    clone. The leaf is the ``_SELF_REPO_HASH`` sentinel."""
    with caplog.at_level(logging.INFO, logger="backend.workspace"):
        info = asyncio.run(ws_mod.provision(
            agent_id="row7-self-agent",
            task_id="row7-self-task",
        ))
    try:
        url_hash_lines = [
            r for r in caplog.records
            if "Workspace url_hash assigned" in r.getMessage()
        ]
        assert url_hash_lines
        msg = url_hash_lines[-1].getMessage()
        assert "remote=<self-repo>" in msg
        assert f"url_hash={ws_mod._SELF_REPO_HASH}" in msg
        assert info.path.name == ws_mod._SELF_REPO_HASH
    finally:
        asyncio.run(ws_mod.cleanup("row7-self-agent"))


# ---------------------------------------------------------------------------
# 2) The audit-bug regression: 3 repos with SAME basename + DIFFERENT URLs
#    must coexist under 3 distinct leaves (the row-1 test covers 2-URL;
#    row 7 expands to 3+ to catch any first-N-only off-by-one regression)
# ---------------------------------------------------------------------------


def test_three_same_basename_different_urls_coexist(
    tmp_path: Path, redirected_ws_root,
):
    """The audit bug: ``agent_X`` cloning ``github.com/A/foo``,
    ``gitlab.com/B/foo``, and ``bitbucket.org/C/foo`` (all named ``foo``)
    pre-Y6 silently overwrote each other at the flat
    ``_WORKSPACES_ROOT/agent_X/`` path. After row 1 each lands on a
    distinct ``sha256(url)[:16]`` leaf. This test extends row 1's 2-URL
    coverage to 3 URLs so any "we only checked the first pair" regression
    surfaces immediately.
    """
    # Three repos all literally named ``foo`` — premise of the audit bug.
    repos = []
    markers = []
    for team_idx in ("a", "b", "c"):
        marker = f"team-{team_idx} content\n"
        path = _make_repo(tmp_path / f"team_{team_idx}" / "foo", marker=marker)
        assert path.name == "foo"  # confirm test premise
        repos.append(path)
        markers.append(marker)

    # Provision them sequentially under the SAME agent_id, popping the
    # registry entry between runs so the per-agent cleanup-on-reprovision
    # doesn't mask the test (the property under test is "different leaf
    # dirs persist on disk", not "the registry juggles them").
    leaves = []
    for repo in repos:
        info = asyncio.run(ws_mod.provision(
            agent_id="row7-multi-repo-agent",
            task_id="row7-task",
            repo_source=str(repo),
        ))
        leaves.append(info.path)
        ws_mod._workspaces.pop("row7-multi-repo-agent", None)

    try:
        # All three leaves must be distinct paths.
        assert len({str(p) for p in leaves}) == 3, (
            "three different URLs must produce three different leaf dirs; "
            f"got {[str(p) for p in leaves]}"
        )
        # And they all share the same {tid}/{pl}/{pid}/{agent_id} parent
        # — only the hash leaf disambiguates.
        parents = {str(p.parent) for p in leaves}
        assert len(parents) == 1, (
            f"expected single shared parent across the three leaves, got "
            f"{parents}"
        )
        # And each leaf still contains its OWN repo's content (no cross-
        # contamination between the three foo's).
        for leaf, marker in zip(leaves, markers):
            assert leaf.is_dir()
            assert (leaf / "README.md").read_text() == marker
        # And each leaf name equals the expected sha256[:16] of its URL.
        for leaf, repo in zip(leaves, repos):
            assert leaf.name == ws_mod._repo_url_hash(str(repo))
    finally:
        asyncio.run(ws_mod.cleanup("row7-multi-repo-agent"))


# ---------------------------------------------------------------------------
# 3) Hash differentiates URLs that share basename even down to the
#    org/team/etc — the only thing that matters is the full URL string
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url_a,url_b",
    [
        # Same host, same org name, different protocol
        (
            "https://github.com/acme/foo.git",
            "ssh://git@github.com/acme/foo.git",
        ),
        # Same basename + same org name, different host
        (
            "https://github.com/acme/foo.git",
            "https://gitlab.com/acme/foo.git",
        ),
        # Same host + same basename, different org
        (
            "https://github.com/teamA/foo.git",
            "https://github.com/teamB/foo.git",
        ),
        # `.git` suffix variant — strict string match, by design
        (
            "https://github.com/acme/foo.git",
            "https://github.com/acme/foo",
        ),
        # Trailing slash variant — strict string match, by design
        (
            "https://github.com/acme/foo.git",
            "https://github.com/acme/foo.git/",
        ),
    ],
)
def test_url_pair_resolves_to_distinct_hashes(url_a: str, url_b: str):
    """Pure-helper level: the hash is sha256(literal URL), so any
    byte-difference in the URL string produces a different leaf. We
    enumerate the realistic "audit bug" variants — same basename, same
    org, different protocol/host/team/suffix — and assert each pair
    routes to a distinct leaf. The hashing function does NOT normalise
    URLs (no .git stripping, no host case-folding) — that's by design,
    because two URL forms can have different credentials / authority.
    """
    h_a = ws_mod._repo_url_hash(url_a)
    h_b = ws_mod._repo_url_hash(url_b)
    assert h_a != h_b, f"expected distinct hash for {url_a} vs {url_b}"
    # And both are 16-char hex (the length is part of the contract —
    # 64 bits of distinguishing entropy).
    assert len(h_a) == len(h_b) == 16
    assert all(c in "0123456789abcdef" for c in h_a + h_b)


# ---------------------------------------------------------------------------
# 4) SSE ``workspace.provisioned`` event detail carries url_hash=
# ---------------------------------------------------------------------------


def test_sse_provisioned_event_carries_url_hash(
    tmp_path: Path, redirected_ws_root, captured_sse,
):
    """The ``workspace`` bus topic gets a ``provisioned`` event whose
    ``detail`` string includes ``url_hash=<hex>``. Dashboards subscribed
    to the bus see the collision-safety leaf assignment without having
    to scrape the full path string."""
    repo = _make_repo(tmp_path / "src" / "sse-repo", marker="X\n")
    expected_hash = ws_mod._repo_url_hash(str(repo))

    info = asyncio.run(ws_mod.provision(
        agent_id="row7-sse-agent",
        task_id="row7-sse-task",
        repo_source=str(repo),
    ))
    try:
        # Filter to the workspace topic ``provisioned`` events. There may
        # be multiple SSE events from one provision (pipeline_phase,
        # agent_update, env checks); we only care about ``workspace``.
        ws_events = [
            payload for topic, payload in captured_sse
            if topic == "workspace" and payload.get("action") == "provisioned"
        ]
        assert ws_events, "expected one workspace.provisioned SSE event"
        detail = ws_events[-1].get("detail", "")
        assert f"url_hash={expected_hash}" in detail, (
            f"SSE provisioned detail must include 'url_hash={expected_hash}'; "
            f"got: {detail!r}"
        )
        # Sanity: the leaf the event reports matches the actual workspace.
        assert info.path.name == expected_hash
    finally:
        asyncio.run(ws_mod.cleanup("row7-sse-agent"))


# ---------------------------------------------------------------------------
# 5) Idempotent — same agent + same URL twice → same leaf, no collision
# ---------------------------------------------------------------------------


def test_same_agent_same_url_yields_same_leaf_path(redirected_ws_root):
    """The dual property of ``different URL → different leaf`` is
    ``same URL → same leaf``. This makes re-provisioning idempotent
    (because the hash is deterministic) and is what lets the GC reaper
    (Y6 row 6) LRU-rank workspaces by ``mtime`` of a stable path.

    Pure-helper level assertion — the deeper double-provision lifecycle
    test lives in ``test_workspace_hierarchy.py``; here we only need to
    pin the property at the path-resolver, which is the contract row 7's
    collision-safety leans on (``_repo_url_hash`` is the LEAF's only
    input, so equal inputs must yield equal leaves)."""
    url = "https://github.com/acme/idempotent-foo.git"
    common = dict(
        tenant_id="t-acme",
        product_line="cameras",
        project_id="proj-x",
        agent_id="row7-idem-agent",
    )
    p_first = ws_mod._workspace_path_for(**common, remote_url=url)
    p_second = ws_mod._workspace_path_for(**common, remote_url=url)
    assert p_first == p_second
    # And the leaf equals the deterministic sha256[:16] of the URL.
    assert p_first.name == ws_mod._repo_url_hash(url)
    # And re-deriving the hash directly produces the same value.
    assert ws_mod._repo_url_hash(url) == ws_mod._repo_url_hash(url)


# ---------------------------------------------------------------------------
# 6) In-repo worktree (no remote_url) collapses to "self" sentinel and
#    does NOT collide with any external URL's hash leaf
# ---------------------------------------------------------------------------


def test_self_repo_leaf_does_not_collide_with_external_url_leaves():
    """``_SELF_REPO_HASH = 'self'`` is a literal string sentinel — by
    construction no real ``sha256(url)[:16]`` (16 hex chars) can equal
    the 4-char string 'self'. This test locks that property: any
    plausible external URL routes to a 16-hex leaf that is provably
    different from the self-leaf.

    Why this matters for the audit bug: an in-repo worktree provisioned
    for ``agent_X`` (e.g. for a chatops dev task) must not silently
    overwrite ``agent_X``'s external clone of some same-basename remote.
    """
    self_hash = ws_mod._repo_url_hash(None)
    assert self_hash == ws_mod._SELF_REPO_HASH == "self"

    # Sample a handful of plausible URLs — none can hash to the literal
    # 4-char string "self" (they are all 16 hex chars by construction).
    sample_urls = [
        "https://github.com/acme/self.git",  # adversarial: basename "self"
        "https://github.com/acme/foo.git",
        "ssh://git@gitlab.com/team/x.git",
        "https://bitbucket.org/team/y",
    ]
    for url in sample_urls:
        h = ws_mod._repo_url_hash(url)
        assert len(h) == 16
        assert h != self_hash, (
            f"hash for {url!r} collided with self sentinel (impossible)"
        )


# ---------------------------------------------------------------------------
# 7) Hash leaf is computed from the post-gerrit-override source — so a
#    gerrit-redirected workspace gets its own dir and does not clobber
#    a previously provisioned non-gerrit clone
# ---------------------------------------------------------------------------


def test_hash_leaf_uses_post_override_source(redirected_ws_root):
    """``provision()`` computes the hash from ``_remote_for_hash`` which
    is set AFTER the gerrit override block. This ensures gerrit and
    non-gerrit provisioning routes produce DIFFERENT leaves even when
    the agent_id and original task are otherwise identical — and so
    they cannot collide on disk.

    Pure-helper assertion: the path resolver routes "same agent, two
    different URLs (one as if pre-gerrit, one as if post-gerrit
    override)" to distinct leaves.
    """
    pre_url = "https://github.com/acme/internal-fw.git"
    post_url = "ssh://git@gerrit.internal:29418/acme/internal-fw"

    p_pre = ws_mod._workspace_path_for(
        tenant_id="t-acme",
        product_line="cameras",
        project_id="proj-x",
        agent_id="row7-gerrit-agent",
        remote_url=pre_url,
    )
    p_post = ws_mod._workspace_path_for(
        tenant_id="t-acme",
        product_line="cameras",
        project_id="proj-x",
        agent_id="row7-gerrit-agent",
        remote_url=post_url,
    )
    assert p_pre != p_post
    assert p_pre.parent == p_post.parent  # same agent_id parent
    assert p_pre.name == ws_mod._repo_url_hash(pre_url)
    assert p_post.name == ws_mod._repo_url_hash(post_url)


# ---------------------------------------------------------------------------
# 8) Pre-clone hash equals the actual on-disk leaf (no path-vs-clone drift)
# ---------------------------------------------------------------------------


def test_pre_clone_hash_matches_on_disk_leaf(
    tmp_path: Path, redirected_ws_root, caplog,
):
    """If the pre-clone log says the leaf is ``<hex_X>`` but the actual
    on-disk leaf is ``<hex_Y>``, an operator's forensic correlation
    breaks. This test pins the invariant: the hash logged BEFORE the
    clone command equals the directory name AFTER the clone returns.
    Catches regressions where someone might re-resolve the path between
    log and clone.
    """
    repo = _make_repo(tmp_path / "src" / "log-vs-disk", marker="LD\n")
    expected = sha256_prefix16 = hashlib.sha256(
        str(repo).encode("utf-8"),
    ).hexdigest()[:16]
    assert expected == ws_mod._repo_url_hash(str(repo))

    with caplog.at_level(logging.INFO, logger="backend.workspace"):
        info = asyncio.run(ws_mod.provision(
            agent_id="row7-logdrift-agent",
            task_id="row7-task",
            repo_source=str(repo),
        ))
    try:
        # Extract the url_hash from the log line.
        prelog = next(
            (r for r in caplog.records
             if "Workspace url_hash assigned" in r.getMessage()),
            None,
        )
        assert prelog is not None
        # Grep the value out of the formatted line.
        msg = prelog.getMessage()
        token = "url_hash="
        idx = msg.index(token) + len(token)
        logged_hash = msg[idx:idx + 16]
        # Both logged and on-disk must equal the expected sha256[:16].
        assert logged_hash == expected
        assert info.path.name == expected
        # And the actual worktree dir must exist.
        assert info.path.is_dir()
    finally:
        asyncio.run(ws_mod.cleanup("row7-logdrift-agent"))


# ---------------------------------------------------------------------------
# 9) Self-fingerprint guard — the new prod code path is compat-clean
# ---------------------------------------------------------------------------


def test_self_fingerprint_clean():
    """SOP Step 3 / pre-commit fingerprint grep, scoped narrowly to the
    Y6 row 7 prod additions: the pre-clone log + SSE detail enhancement
    inside ``provision``. None of the four legacy compat fingerprints
    (``_conn()`` / ``await conn.commit()`` / ``datetime('now')`` /
    SQLite ``?`` placeholders) should appear in those lines.
    """
    import inspect
    import re

    src = inspect.getsource(ws_mod.provision)
    # Restrict to the row 7 region — bracketed by the row 7 tag and the
    # subsequent SSE emit.
    start = src.index("Y6 #282 row 7 — explicit pre-clone")
    end = src.index("emit_agent_update(agent_id, \"running\"")
    region = src[start:end]

    fingerprint = re.compile(
        r"_conn\(\)|await conn\.commit\(\)|datetime\('now'\)|VALUES.*\?[,)]",
    )
    matches = fingerprint.findall(region)
    assert not matches, (
        f"row 7 prod region tripped fingerprint grep: {matches!r}"
    )
