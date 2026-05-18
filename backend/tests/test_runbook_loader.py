"""WP.8 — Runbook loader, synthesizer, executor contracts (OP-1502).

Locks (matches the AC list on the ticket):

  * YAML loader honours 3-scope precedence (project > home > bundled).
  * Block→Runbook synthesizer slugs the name, derives params from
    ``{{ var }}`` placeholders, and pins ``source_url`` to the block id
    so WP.1 lineage trace survives the round-trip.
  * Executor chains output blocks via ``parent_id`` and refuses to run
    when required params are missing (the parameter-prompting UI relies
    on this for re-prompt).
  * Round-trip: synthesize → write to project scope → reload → execute
    with new params. Verifies the exercised-AC flow from the ticket.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.agents import runbook_loader as rl
from backend.models import Block


# ─── Parser ──────────────────────────────────────────────────────


def test_parse_runbook_text_extracts_full_shape(tmp_path: Path) -> None:
    text = (
        "name: bring-up-rk3588\n"
        "description: RK3588 SoC bring-up checklist\n"
        "tags: [hd, bring-up]\n"
        "source_url: omnisight://block/blk-abc\n"
        "params:\n"
        "  - name: target_soc\n"
        "    type: string\n"
        "    default: rk3588\n"
        "    description: SoC mark\n"
        "  - name: enable_pcie\n"
        "    type: bool\n"
        "    default: false\n"
        "steps:\n"
        "  - kind: command\n"
        "    title: Probe USB\n"
        "    command: lsusb | grep {{ target_soc }}\n"
        "  - kind: prompt\n"
        "    title: Confirm boot\n"
        "    prompt: Boot reached login?\n"
    )
    rb = rl.parse_runbook_text(text, scope="project")
    assert rb is not None
    assert rb.name == "bring-up-rk3588"
    assert rb.description.startswith("RK3588")
    assert rb.tags == ("hd", "bring-up")
    assert rb.source_url == "omnisight://block/blk-abc"
    assert [p.name for p in rb.params] == ["target_soc", "enable_pcie"]
    assert rb.params[0].default == "rk3588"
    assert rb.params[1].type == "bool"
    # default present → required defaults to False
    assert rb.params[1].required is False
    assert [s.kind for s in rb.steps] == ["command", "prompt"]
    assert rb.steps[0].payload["command"].endswith("{{ target_soc }}")


def test_parse_runbook_text_rejects_non_mapping(tmp_path: Path) -> None:
    # Bare scalar is not a valid runbook
    assert rl.parse_runbook_text("just a string\n", scope="project") is None
    # Empty string yields None, not a malformed Runbook
    assert rl.parse_runbook_text("", scope="project") is None


def test_parse_runbook_normalises_unknown_step_kind(tmp_path: Path) -> None:
    text = (
        "name: r1\n"
        "steps:\n"
        "  - kind: WAT\n"
        "    title: weird\n"
        "    extra: thing\n"
    )
    rb = rl.parse_runbook_text(text, scope="project")
    assert rb is not None
    assert rb.steps[0].kind == "comment"  # unknown kind normalised
    assert rb.steps[0].payload == {"extra": "thing"}


def test_parse_runbook_yaml_error_returns_none(caplog) -> None:
    bad = "name: rb\nsteps: [unclosed\n"
    with caplog.at_level("WARNING", logger="backend.agents.runbook_loader"):
        rb = rl.parse_runbook_text(bad, scope="project")
    assert rb is None
    assert any("YAML parse failed" in r.message for r in caplog.records)


def test_param_coerce_respects_declared_type() -> None:
    p = rl.RunbookParam(name="n", type="int", default=0)
    assert p.coerce("42") == 42
    assert p.coerce("not-a-number") == "not-a-number"  # fallback
    pb = rl.RunbookParam(name="b", type="bool", default=False)
    assert pb.coerce("yes") is True
    assert pb.coerce("no") is False
    assert pb.coerce(0) is False
    pf = rl.RunbookParam(name="f", type="float", default=0.0)
    assert pf.coerce("3.14") == pytest.approx(3.14)


# ─── 3-scope precedence ──────────────────────────────────────────


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_load_default_scopes_walks_three_scopes(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    home = tmp_path / "home"

    _write(
        project / ".omnisight" / "runbooks" / "alpha.yaml",
        "name: alpha\ndescription: from-project\nsteps: []\n",
    )
    _write(
        home / ".omnisight" / "runbooks" / "beta.yaml",
        "name: beta\ndescription: from-home\nsteps: []\n",
    )
    _write(
        project / "configs" / "runbooks" / "gamma.yaml",
        "name: gamma\ndescription: from-bundled\nsteps: []\n",
    )

    reg = rl.load_default_scopes(project, home=home)
    assert reg.names() == ["alpha", "beta", "gamma"]
    assert reg.get("alpha").scope == "project"
    assert reg.get("beta").scope == "home"
    assert reg.get("gamma").scope == "bundled"


def test_project_shadows_home_and_bundled(tmp_path: Path) -> None:
    """Same name in higher scope wins — matches the WP.2 shadowing rule."""
    project = tmp_path / "proj"
    home = tmp_path / "home"

    _write(
        project / ".omnisight" / "runbooks" / "overlap.yaml",
        "name: overlap\ndescription: project-wins\nsteps: []\n",
    )
    _write(
        home / ".omnisight" / "runbooks" / "overlap.yaml",
        "name: overlap\ndescription: home-loses\nsteps: []\n",
    )
    _write(
        project / "configs" / "runbooks" / "overlap.yaml",
        "name: overlap\ndescription: bundled-loses\nsteps: []\n",
    )

    reg = rl.load_default_scopes(project, home=home)
    assert reg.get("overlap").description == "project-wins"
    assert reg.get("overlap").scope == "project"


def test_loader_ignores_non_yaml_files(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    _write(
        project / ".omnisight" / "runbooks" / "real.yaml",
        "name: real\nsteps: []\n",
    )
    _write(
        project / ".omnisight" / "runbooks" / "README.md",
        "# not a runbook\n",
    )
    reg = rl.load_default_scopes(project, home=tmp_path / "home-empty")
    assert reg.names() == ["real"]


# ─── Synthesizer ─────────────────────────────────────────────────


def _block(**overrides) -> Block:
    defaults: dict = dict(
        block_id="blk-1234567890ab",
        tenant_id="t-1",
        user_id="u-1",
        kind="command",
        status="completed",
        title="Probe USB device {{ target_soc }}",
        payload={
            "command": "lsusb | grep {{ target_soc }}",
            "args": {"timeout_sec": 30, "shell": True},
        },
        metadata={},
    )
    defaults.update(overrides)
    return Block(**defaults)


def test_synthesize_runbook_extracts_placeholders_as_params() -> None:
    rb = rl.synthesize_runbook_from_block(_block())
    # Free placeholder ``{{ target_soc }}`` lifts into a param.
    assert [p.name for p in rb.params] == ["target_soc"]
    assert rb.params[0].type == "string"
    assert rb.params[0].required is True
    # Lineage trace: source_url pins the originating block id.
    assert rb.source_url == "omnisight://block/blk-1234567890ab"
    # Step kind matches the block kind because ``command`` is in
    # VALID_STEP_KINDS.
    assert rb.steps[0].kind == "command"
    # Free non-text payload fields are preserved verbatim so re-execute
    # keeps the original block's options (timeouts, shell flags, ...).
    assert rb.steps[0].payload["args"] == {"timeout_sec": 30, "shell": True}


def test_synthesize_runbook_slugs_name() -> None:
    rb = rl.synthesize_runbook_from_block(
        _block(title="Probe USB!! Device 🔌 RK3588"),
    )
    # Slug: lowercase, dashes, no emoji, no double-dash runs.
    assert rb.name == "probe-usb-device-rk3588"


def test_synthesize_runbook_falls_back_to_comment_for_unknown_kind() -> None:
    rb = rl.synthesize_runbook_from_block(_block(kind="snapshot"))
    # 'snapshot' is a valid WP.1 block kind but not a runbook step kind,
    # so the synthesised step falls back to ``comment``.
    assert rb.steps[0].kind == "comment"


def test_runbook_to_yaml_round_trips() -> None:
    rb = rl.synthesize_runbook_from_block(_block())
    yaml_body = rl.runbook_to_yaml(rb)
    reloaded = rl.parse_runbook_text(yaml_body, scope="project")
    assert reloaded is not None
    assert reloaded.name == rb.name
    assert reloaded.source_url == rb.source_url
    assert [p.name for p in reloaded.params] == [p.name for p in rb.params]
    assert reloaded.steps[0].kind == rb.steps[0].kind


# ─── Execution ───────────────────────────────────────────────────


def _two_step_runbook() -> rl.Runbook:
    return rl.Runbook(
        name="chain-demo",
        description="two-step chain",
        params=(
            rl.RunbookParam(name="target", type="string", required=True),
            rl.RunbookParam(
                name="retries", type="int", default=3, required=False,
            ),
        ),
        steps=(
            rl.RunbookStep(
                kind="command",
                title="probe {{ target }}",
                payload={"command": "lsusb | grep {{ target }}"},
            ),
            rl.RunbookStep(
                kind="prompt",
                title="confirm",
                payload={"prompt": "Saw {{ target }} ({{ retries }} retries)?"},
            ),
        ),
    )


def test_execute_runbook_chains_blocks_via_parent_id() -> None:
    rb = _two_step_runbook()
    chain = rl.execute_runbook(
        rb,
        supplied_params={"target": "rk3588"},
        tenant_id="t-1",
        user_id="u-1",
        parent_block_id="blk-root",
    )
    assert len(chain) == 2
    assert chain[0].parent_id == "blk-root"
    assert chain[1].parent_id == chain[0].block_id
    # Substitution honoured both supplied and defaulted params.
    assert chain[0].payload["command"] == "lsusb | grep rk3588"
    assert "rk3588" in chain[1].payload["prompt"]
    assert "3 retries" in chain[1].payload["prompt"]
    # First block stamps the runbook descriptor for downstream tracing.
    assert chain[0].payload["runbook"]["name"] == "chain-demo"
    assert chain[0].payload["runbook"]["params"] == {
        "target": "rk3588",
        "retries": 3,
    }
    # Both blocks are kind=runbook_step per WP.1 enumeration.
    assert chain[0].kind == "runbook_step"
    assert chain[1].kind == "runbook_step"
    # Original step kind is preserved for the renderer.
    assert chain[0].payload["step_kind"] == "command"
    assert chain[1].payload["step_kind"] == "prompt"


def test_execute_runbook_refuses_when_required_params_missing() -> None:
    rb = _two_step_runbook()
    with pytest.raises(rl.RunbookExecutionError) as excinfo:
        rl.execute_runbook(
            rb, supplied_params={}, tenant_id="t-1",
        )
    assert "target" in str(excinfo.value)


def test_resolve_params_reports_missing_for_prompt_ui() -> None:
    rb = _two_step_runbook()
    resolved, missing = rl.resolve_params(rb, {})
    assert missing == ["target"]
    # default applied for non-required params.
    assert resolved["retries"] == 3


# ─── Exercised-AC integration: synth → save → reload → execute ───


def test_exercised_round_trip_via_project_scope(tmp_path: Path) -> None:
    """Mirrors AC #4: save 1 block as runbook + re-execute with new params."""
    block = _block(
        title="Probe target {{ target_soc }}",
        payload={"command": "lsusb | grep {{ target_soc }}"},
    )
    rb = rl.synthesize_runbook_from_block(block)
    target_path = rl.write_project_runbook(rb, project_root=tmp_path)
    assert target_path.exists()
    assert target_path.parent.name == "runbooks"
    assert target_path.parent.parent.name == ".omnisight"

    # Re-execute with a different param value.
    reg = rl.load_default_scopes(tmp_path, home=tmp_path / "home-empty")
    reloaded = reg.get(rb.name)
    assert reloaded is not None

    chain_a = rl.execute_runbook(
        reloaded, {"target_soc": "rk3588"}, tenant_id="t-1",
    )
    chain_b = rl.execute_runbook(
        reloaded, {"target_soc": "imx8mp"}, tenant_id="t-1",
    )
    assert chain_a[0].payload["command"] == "lsusb | grep rk3588"
    assert chain_b[0].payload["command"] == "lsusb | grep imx8mp"
    # Lineage source_url preserved through the round-trip.
    assert chain_a[0].payload["runbook"]["source_url"] == (
        f"omnisight://block/{block.block_id}"
    )


def test_write_project_runbook_refuses_overwrite(tmp_path: Path) -> None:
    rb = rl.synthesize_runbook_from_block(_block())
    rl.write_project_runbook(rb, project_root=tmp_path)
    with pytest.raises(FileExistsError):
        rl.write_project_runbook(rb, project_root=tmp_path)
    # overwrite=True succeeds
    rl.write_project_runbook(rb, project_root=tmp_path, overwrite=True)
