"""OP-1061 — runner releases fenced JIRA claims after post-CLI exits."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from backend.agents import jira_dispatch  # noqa: E402


def _load_runner():
    path = REPO_ROOT / "auto-runner-jira.py"
    spec = importlib.util.spec_from_file_location("auto_runner_jira", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_release_wrapper_passes_fencing_token(monkeypatch) -> None:
    runner = _load_runner()
    calls: list[tuple[str, str, str, str | None]] = []
    claim = jira_dispatch.ClaimResult(
        ok=True,
        lost_to=None,
        claim_token="default:0001715518859123456-3af9c1d0",
    )

    monkeypatch.setattr(runner, "INSTANCE_ID", "default")
    monkeypatch.setattr(
        runner.jira_dispatch,
        "release_ticket_claim",
        lambda client, key, instance_id, token=None: calls.append(
            (client, key, instance_id, token)
        ),
    )

    runner._release_ticket_claim_if_acquired("client", "OP-1061", claim)

    assert calls == [
        ("client", "OP-1061", "default", "0001715518859123456-3af9c1d0")
    ]


def test_pre_claim_exit_does_not_release(monkeypatch) -> None:
    runner = _load_runner()
    calls: list[Any] = []
    monkeypatch.setattr(
        runner.jira_dispatch,
        "release_ticket_claim",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    runner._release_ticket_claim_if_acquired("client", "OP-1061", None)
    runner._release_ticket_claim_if_acquired(
        "client",
        "OP-1061",
        jira_dispatch.ClaimResult(ok=False, lost_to="claim:other:tok"),
    )

    assert calls == []


def test_double_release_is_noop_after_first_put(monkeypatch) -> None:
    labels = ["claim:default:tok-a", "claim:default:tok-b", "area:backend"]
    puts: list[list[str]] = []

    def fake_request(client, method, path, body=None):
        nonlocal labels
        if method == "GET":
            return {"fields": {"labels": list(labels)}}
        removed = [item["remove"] for item in body["update"]["labels"]]
        puts.append(removed)
        labels = [label for label in labels if label not in removed]
        return {}

    monkeypatch.setattr(jira_dispatch, "_request", fake_request)

    jira_dispatch.release_ticket_claim("client", "OP-1061", "default", "tok-a")
    jira_dispatch.release_ticket_claim("client", "OP-1061", "default", "tok-a")

    assert puts == [["claim:default:tok-a", "claim:default:tok-b"]]
    assert labels == ["area:backend"]


def test_enumerated_exit_paths_release_after_claim() -> None:
    source = (REPO_ROOT / "auto-runner-jira.py").read_text()
    required = [
        "_finalize_under_review(\n"
        "        client,\n"
        "        key,\n"
        "        push_result.change_url,",
        "_release_ticket_claim_if_acquired(client, key, claim)",
        "except jira_dispatch.NoCommitsOnBranchError as e:",
        "except jira_dispatch.WorktreeDirtyError as e:",
        "except Exception as e:\n"
        "            print(f\"[runner] Gerrit push setup failed:",
        "except outcomes_consumer.OutcomesGraderRefused:\n"
        "                _release_ticket_claim_if_acquired(client, snapshot.key, claim)",
        "if outcomes_status == \"fail\":",
        "_handle_gerrit_push_failure(\n"
        "                client,\n"
        "                snapshot.key,",
    ]
    for needle in required:
        assert needle in source

    assert source.count("_release_ticket_claim_if_acquired") >= 9


def test_area_label_rejection_remains_pre_claim_noop() -> None:
    source = (REPO_ROOT / "auto-runner-jira.py").read_text()
    area_block = source.split("except UnknownAreaLabelError as e:", 1)[1].split(
        "enabled_caps =", 1
    )[0]
    assert "claim_ticket_atomic" not in area_block
    assert "_release_ticket_claim_if_acquired" not in area_block
