"""Unit coverage for intent router branch behavior."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from backend import auth as _au
from backend import intent_parser as _ip
from backend.routers import intent as intent_router


def _operator() -> _au.User:
    return _au.User(
        id="user-operator",
        email="operator@test.local",
        name="operator",
        role="operator",
        tenant_id="tenantA",
    )


def test_hydrate_parsed_from_dict_defaults_bad_entries() -> None:
    parsed = intent_router._hydrate_parsed_from_dict(
        {
            "project_type": "not-a-field-dict",
            "runtime_model": {"value": "ssr", "confidence": "1.5"},
            "target_arch": {"value": "", "confidence": "not-a-float"},
            "target_os": None,
            "hardware_required": {"value": "", "confidence": None},
            "raw_text": None,
        }
    )

    assert parsed.project_type == _ip.Field("unknown", 0.0)
    assert parsed.runtime_model == _ip.Field("ssr", 1.0)
    assert parsed.target_arch == _ip.Field("unknown", 0.0)
    assert parsed.target_os == _ip.Field("linux", 0.3)
    assert parsed.hardware_required == _ip.Field("no", 0.3)
    assert parsed.raw_text == ""


@pytest.mark.asyncio
async def test_clarify_ignores_memory_record_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    updated = _ip.ParsedSpec(
        runtime_model=_ip.Field("ssr", 1.0),
        raw_text="Build a static site with a runtime database.",
    )
    annotated: list[tuple[str, list[_ip.SpecConflict]]] = []

    def fake_apply(
        parsed: _ip.ParsedSpec,
        conflict_id: str,
        option_id: str,
    ) -> _ip.ParsedSpec:
        assert parsed.raw_text == "Build a static site with a runtime database."
        assert conflict_id == "static_with_runtime_db"
        assert option_id == "ssr_runtime"
        return updated

    async def fail_record(**_kwargs: object) -> None:
        raise RuntimeError("memory offline")

    async def fake_annotate(raw_text: str, conflicts: list[_ip.SpecConflict]) -> None:
        annotated.append((raw_text, conflicts))

    monkeypatch.setattr(intent_router._ip, "apply_clarification", fake_apply)
    monkeypatch.setattr(intent_router._imem, "record_clarification_choice", fail_record)
    monkeypatch.setattr(intent_router._imem, "annotate_conflicts_with_priors", fake_annotate)

    body = await intent_router.clarify(
        intent_router.ClarifyRequest(
            parsed={
                "raw_text": "Build a static site with a runtime database.",
                "conflicts": [],
            },
            conflict_id="static_with_runtime_db",
            option_id="ssr_runtime",
        ),
        _user=_operator(),
    )

    assert body["runtime_model"] == {"value": "ssr", "confidence": 1.0}
    assert annotated == [("Build a static site with a runtime database.", [])]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exc", "status_code"),
    [
        (ValueError("bad url"), 422),
        (PermissionError("private repo"), 403),
        (RuntimeError("clone failed"), 502),
    ],
)
async def test_ingest_repo_maps_errors_to_http_status(
    monkeypatch: pytest.MonkeyPatch,
    exc: Exception,
    status_code: int,
) -> None:
    from backend import repo_ingest

    async def fail_ingest(_url: str) -> tuple[_ip.ParsedSpec, SimpleNamespace]:
        raise exc

    monkeypatch.setattr(repo_ingest, "ingest_repo", fail_ingest)

    with pytest.raises(HTTPException) as raised:
        await intent_router.ingest_repo(
            intent_router.IngestRepoRequest(url="https://example.test/repo.git"),
            _user=_operator(),
        )

    assert raised.value.status_code == status_code
    assert raised.value.detail == str(exc)


class _Upload:
    def __init__(self, filename: str, content: bytes) -> None:
        self.filename = filename
        self._content = content

    async def read(self) -> bytes:
        return self._content


@pytest.mark.asyncio
async def test_upload_docs_returns_file_results_when_every_file_rejected() -> None:
    response = await intent_router.upload_docs(
        files=[
            _Upload("installer.exe", b"ignored"),
            _Upload("large.md", b"x" * (intent_router._MAX_DOC_SIZE + 1)),
        ],
        _user=_operator(),
    )

    assert response == {
        "spec": None,
        "files": [
            {
                "name": "installer.exe",
                "status": "rejected",
                "reason": "unsupported extension: .exe",
            },
            {
                "name": "large.md",
                "status": "rejected",
                "reason": "file too large (>2MB)",
            },
        ],
    }


@pytest.mark.asyncio
async def test_upload_docs_parses_combined_accepted_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    async def fake_parse_intent(
        text: str,
        *,
        ask_fn: _ip.AskFn | None = None,
        model: str = "",
    ) -> _ip.ParsedSpec:
        captured["text"] = text
        captured["ask_fn"] = ask_fn
        captured["model"] = model
        return _ip.ParsedSpec(
            framework=_ip.Field("nextjs", 0.9),
            raw_text=text,
        )

    monkeypatch.setattr(intent_router._ip, "parse_intent", fake_parse_intent)

    response = await intent_router.upload_docs(
        files=[
            _Upload("brief.md", b"Build a Next.js dashboard"),
            _Upload("settings.json", b'{"runtime": "ssr"}'),
        ],
        _user=_operator(),
    )

    assert captured["text"] == (
        "[from brief.md]\nBuild a Next.js dashboard\n---\n"
        '[from settings.json]\n{"runtime": "ssr"}'
    )
    assert response["spec"]["framework"] == {"value": "nextjs", "confidence": 0.9}
    assert response["files"] == [
        {"name": "brief.md", "status": "parsed", "size": 25},
        {"name": "settings.json", "status": "parsed", "size": 18},
    ]
