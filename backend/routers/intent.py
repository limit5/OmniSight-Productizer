"""Phase 68-C — Intent Parser + Clarification HTTP surface.

Endpoints:

  POST /api/v1/intent/parse
  POST /api/v1/intent/clarify
  POST /api/v1/intent/ingest-repo   (B5/UX-01)
  POST /api/v1/intent/upload-docs   (B5/UX-01)

The SpecTemplateEditor UI is the primary consumer.
Authenticated — require_operator — so these can't be hit by a
bot-harvester probing for the free LLM backend.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from pydantic import BaseModel, Field

from backend import auth as _au
from backend import intent_parser as _ip
from backend import intent_memory as _imem

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/intent", tags=["intent"])


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Shared helper
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _hydrate_parsed_from_dict(data: dict) -> _ip.ParsedSpec:
    """Reverse of ParsedSpec.to_dict() — used by /clarify so the
    client doesn't have to POST the raw text + re-parse round-trip."""
    def f(name: str, default_v: str = "unknown", default_c: float = 0.0) -> _ip.Field:
        entry = (data.get(name) or {})
        if not isinstance(entry, dict):
            return _ip.Field(default_v, default_c)
        v = str(entry.get("value") or default_v)
        try:
            c = max(0.0, min(1.0, float(entry.get("confidence") or default_c)))
        except (TypeError, ValueError):
            c = default_c
        return _ip.Field(v, c)

    return _ip.ParsedSpec(
        project_type=f("project_type"),
        runtime_model=f("runtime_model"),
        target_arch=f("target_arch"),
        target_os=f("target_os", "linux", 0.3),
        framework=f("framework"),
        persistence=f("persistence"),
        deploy_target=f("deploy_target"),
        hardware_required=f("hardware_required", "no", 0.3),
        raw_text=str(data.get("raw_text") or ""),
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  POST /intent/parse
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class ParseRequest(BaseModel):
    text: str = Field(..., description="Free-form operator command")
    use_llm: bool = Field(
        True,
        description="If false, skip the LLM path and use the regex "
                    "heuristic only (faster, cheaper, no token spend).",
    )


@router.post("/parse")
async def parse(req: ParseRequest,
                _user=Depends(_au.require_operator)) -> dict:
    """Parse a free-form command into a structured ParsedSpec dict."""
    ask_fn = None
    model = ""
    if req.use_llm:
        try:
            from backend.iq_runner import live_ask_fn
            from backend.config import settings as _s
            ask_fn = live_ask_fn
            model = f"{_s.llm_provider}/{_s.get_model_name()}"
        except Exception as exc:
            logger.debug("intent/parse: LLM unavailable, heuristic path: %s", exc)

    parsed = await _ip.parse_intent(req.text, ask_fn=ask_fn, model=model)
    body = parsed.to_dict()
    # Phase 68-D: annotate each conflict with a `prior_choice` hint
    # when L3 has a matching record. The UI pre-selects the option
    # but still requires an explicit click (we deliberately don't
    # silently steer the operator's current intent).
    await _imem.annotate_conflicts_with_priors(req.text, body["conflicts"])
    return body


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  POST /intent/clarify
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class ClarifyRequest(BaseModel):
    parsed: dict = Field(..., description="Current ParsedSpec JSON")
    conflict_id: str = Field(..., min_length=1)
    option_id: str = Field(..., min_length=1)


@router.post("/clarify")
async def clarify(req: ClarifyRequest,
                  _user=Depends(_au.require_operator)) -> dict:
    """Apply an operator's clarification choice and return the
    updated ParsedSpec (with conflicts re-detected)."""
    ps = _hydrate_parsed_from_dict(req.parsed)
    updated = _ip.apply_clarification(ps, req.conflict_id, req.option_id)
    if updated is ps:
        # apply_clarification returns the input unchanged when the
        # ids are unknown. Surface that as a 422 so stale-tab clicks
        # fail loudly rather than silently doing nothing.
        raise HTTPException(
            status_code=422,
            detail=f"unknown conflict_id={req.conflict_id!r} or "
                   f"option_id={req.option_id!r}",
        )

    # Phase 68-D: persist the operator's pick to L3 so next parse
    # of a similar prompt carries a `prior_choice` hint. Best-
    # effort — failure must not block the clarification response.
    try:
        await _imem.record_clarification_choice(
            raw_text=ps.raw_text,
            conflict_id=req.conflict_id,
            option_id=req.option_id,
            operator_email=getattr(_user, "email", None),
        )
    except Exception as exc:
        logger.debug("intent/clarify: memory record failed: %s", exc)

    body = updated.to_dict()
    # Re-annotate — a subsequent conflict (second round) should
    # carry its own prior hint if one exists.
    await _imem.annotate_conflicts_with_priors(ps.raw_text, body["conflicts"])
    return body


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  POST /intent/ingest-repo (B5/UX-01)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class IngestRepoRequest(BaseModel):
    url: str = Field(..., min_length=1, description="Git clone URL")


@router.post("/ingest-repo")
async def ingest_repo(req: IngestRepoRequest,
                      _user=Depends(_au.require_operator)) -> dict:
    """Clone a repo, introspect manifests, return a ParsedSpec."""
    from backend.repo_ingest import ingest_repo as _ingest
    try:
        spec, intro = await _ingest(req.url)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    body = spec.to_dict()
    body["_ingest_meta"] = {
        "detected_files": intro.detected_files,
        "has_package_json": intro.package_json is not None,
        "has_readme": bool(intro.readme_content),
        "has_requirements": len(intro.requirements_txt) > 0,
        "has_cargo": bool(intro.cargo_toml),
    }
    return body


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  POST /intent/upload-docs (B5/UX-01)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_ALLOWED_DOC_EXTENSIONS = {".txt", ".md", ".json", ".yaml", ".yml", ".toml", ".cfg", ".ini", ".csv"}
_MAX_DOC_SIZE = 2 * 1024 * 1024  # 2 MB per file


@router.post("/upload-docs")
async def upload_docs(
    files: list[UploadFile] = File(...),
    _user=Depends(_au.require_operator),
) -> dict:
    """Parse uploaded doc files and return a merged ParsedSpec."""
    if not files:
        raise HTTPException(status_code=422, detail="No files uploaded")

    file_results: list[dict] = []
    combined_text_parts: list[str] = []

    for uf in files:
        name = uf.filename or "unknown"
        ext = Path(name).suffix.lower()
        if ext not in _ALLOWED_DOC_EXTENSIONS:
            file_results.append({"name": name, "status": "rejected", "reason": f"unsupported extension: {ext}"})
            continue

        content = await uf.read()
        if len(content) > _MAX_DOC_SIZE:
            file_results.append({"name": name, "status": "rejected", "reason": "file too large (>2MB)"})
            continue

        try:
            text = content.decode("utf-8", errors="replace")
        except Exception:
            file_results.append({"name": name, "status": "error", "reason": "decode failed"})
            continue

        combined_text_parts.append(f"[from {name}]\n{text[:4096]}")
        file_results.append({"name": name, "status": "parsed", "size": len(content)})

    if not combined_text_parts:
        return {"spec": None, "files": file_results}

    combined_text = "\n---\n".join(combined_text_parts)

    ask_fn = None
    model = ""
    try:
        from backend.iq_runner import live_ask_fn
        from backend.config import settings as _s
        ask_fn = live_ask_fn
        model = f"{_s.llm_provider}/{_s.get_model_name()}"
    except Exception as exc:
        logger.debug("intent/upload-docs: LLM unavailable: %s", exc)

    parsed = await _ip.parse_intent(combined_text, ask_fn=ask_fn, model=model)
    body = parsed.to_dict()
    return {"spec": body, "files": file_results}
