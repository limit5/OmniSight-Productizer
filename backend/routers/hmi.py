"""C26 — L4-CORE-26 HMI embedded web UI framework endpoints (#261).

REST endpoints exposing the constrained generator, bundle budget gate,
IEC 62443 security scan, ABI matrix query, i18n catalog, shared
component library, and NL + HAL binding generator.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend import auth as _au
from backend import hmi_binding as _hb
from backend import hmi_components as _hc
from backend import hmi_framework as _hf
from backend import hmi_generator as _hg
from backend import hmi_llm as _hl

_require = _au.require_operator

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/hmi", tags=["hmi"])


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Request models
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class BudgetCheckRequest(BaseModel):
    platform: str = Field(..., description="Target platform (aarch64 / armv7 / ...)")
    files: dict[str, str] = Field(..., description="{path: UTF-8 text content}")


class SecurityScanRequest(BaseModel):
    html: str = Field(default="")
    js: str = Field(default="")
    headers: dict[str, str] = Field(default_factory=dict)
    csp: str = Field(default="")


class ABICheckRequest(BaseModel):
    platform: str
    needs: dict[str, bool] = Field(default_factory=dict)
    needs_es_version: str = Field(default="ES2019")


class PageSectionReq(BaseModel):
    id: str
    title: str
    kind: str = "form"
    fields: list[dict[str, Any]] = Field(default_factory=list)


class GenerateRequest(BaseModel):
    product_name: str
    framework: str = "vanilla"
    platform: str = "aarch64"
    locale: str = "en"
    title_key: str = "nav.home"
    sections: list[PageSectionReq] = Field(default_factory=list)
    extra_scripts: str = ""
    extra_styles: str = ""
    i18n_overrides: dict[str, dict[str, str]] = Field(default_factory=dict)


class HALFieldReq(BaseModel):
    name: str
    c_type: str
    required: bool = True
    max_len: int = 128
    description: str = ""


class HALEndpointReq(BaseModel):
    id: str
    method: str
    path: str
    request_fields: list[HALFieldReq] = Field(default_factory=list)
    response_fields: list[HALFieldReq] = Field(default_factory=list)
    description: str = ""


class BindingGenerateRequest(BaseModel):
    nl_prompt: str
    endpoint: HALEndpointReq
    server: str = "mongoose"
    use_llm: bool = True


class AssembleRequest(BaseModel):
    components: list[str]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Framework metadata
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@router.get("/summary", dependencies=[Depends(_require)])
async def get_summary() -> dict[str, Any]:
    return {
        "framework": _hf.framework_summary(),
        "generator": _hg.summary(),
        "binding": _hb.summary(),
        "components": _hc.summary(),
        "llm": _hl.summary(),
    }


@router.get("/platforms", dependencies=[Depends(_require)])
async def get_platforms() -> list[dict[str, Any]]:
    out = []
    for p in _hf.list_platforms():
        budget = _hf.get_bundle_budget(p)
        out.append({
            "platform": p,
            "flash_partition_bytes": budget.flash_partition_bytes,
            "hmi_budget_bytes": budget.hmi_budget_bytes,
            "html_css_max_bytes": budget.html_css_max_bytes,
            "js_max_bytes": budget.js_max_bytes,
            "fonts_max_bytes": budget.fonts_max_bytes,
        })
    return out


@router.get("/abi-matrix", dependencies=[Depends(_require)])
async def get_abi_matrix() -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for platform, entries in _hf.all_abi_matrix().items():
        result[platform] = [
            {
                "engine": e.engine,
                "version": e.version,
                "min_es_version": e.min_es_version,
                "supports_webgl2": e.supports_webgl2,
                "supports_wasm": e.supports_wasm,
                "supports_webrtc": e.supports_webrtc,
                "notes": e.notes,
            }
            for e in entries
        ]
    return result


@router.post("/abi-check", dependencies=[Depends(_require)])
async def post_abi_check(req: ABICheckRequest) -> dict[str, Any]:
    return _hf.check_abi_compatibility(req.platform, req.needs, req.needs_es_version)


@router.get("/locales", dependencies=[Depends(_require)])
async def get_locales() -> dict[str, Any]:
    return {
        "default": _hf.default_locale(),
        "supported": [
            {"code": loc.code, "name": loc.name, "rtl": loc.rtl}
            for loc in _hf.list_locales()
        ],
    }


@router.get("/i18n-catalog", dependencies=[Depends(_require)])
async def get_i18n_catalog() -> dict[str, dict[str, str]]:
    return _hf.build_i18n_catalog()


@router.get("/frameworks", dependencies=[Depends(_require)])
async def get_frameworks() -> dict[str, Any]:
    return {
        "allowed": [
            {"name": f.name, "version": f.version, "size_bytes": f.size_bytes, "license": f.license}
            for f in _hf.list_allowed_frameworks()
        ],
        "forbidden": _hf.list_forbidden_frameworks(),
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Generator + gates
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@router.post("/generate", dependencies=[Depends(_require)])
async def post_generate(req: GenerateRequest) -> dict[str, Any]:
    try:
        sections = [
            _hg.PageSection(id=s.id, title=s.title, kind=s.kind, fields=s.fields)
            for s in req.sections
        ]
        gen_req = _hg.GeneratorRequest(
            product_name=req.product_name,
            framework=req.framework,
            platform=req.platform,
            locale=req.locale,
            title_key=req.title_key,
            sections=sections,
            extra_scripts=req.extra_scripts,
            extra_styles=req.extra_styles,
            i18n_overrides=req.i18n_overrides,
        )
        bundle = _hg.generate_bundle(gen_req)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "files": bundle.files,
        "headers": bundle.headers,
        "framework": bundle.framework,
        "platform": bundle.platform,
        "total_bytes": bundle.total_bytes,
        "budget_bytes": bundle.budget_bytes,
        "security_status": bundle.security_status,
        "security_findings": bundle.security_findings,
        "budget_violations": bundle.budget_violations,
    }


@router.post("/budget-check", dependencies=[Depends(_require)])
async def post_budget_check(req: BudgetCheckRequest) -> dict[str, Any]:
    try:
        measurement = _hf.measure_bundle(req.files)
        verdict = _hf.check_bundle_budget(req.platform, measurement)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        "platform": verdict.platform,
        "status": verdict.status,
        "total_bytes": verdict.total_bytes,
        "budget_bytes": verdict.budget_bytes,
        "violations": verdict.violations,
        "measurement": {
            "html_bytes": measurement.html_bytes,
            "css_bytes": measurement.css_bytes,
            "js_bytes": measurement.js_bytes,
            "fonts_bytes": measurement.fonts_bytes,
            "other_bytes": measurement.other_bytes,
        },
    }


@router.post("/security-scan", dependencies=[Depends(_require)])
async def post_security_scan(req: SecurityScanRequest) -> dict[str, Any]:
    report = _hf.scan_security(req.html, req.js, req.headers, req.csp)
    return {
        "status": report.status,
        "standard": report.standard,
        "findings": [
            {"severity": f.severity, "rule": f.rule, "detail": f.detail}
            for f in report.findings
        ],
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Binding generator
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@router.post("/binding/generate", dependencies=[Depends(_require)])
async def post_binding(req: BindingGenerateRequest) -> dict[str, Any]:
    try:
        endpoint = _hb.HALEndpoint(
            id=req.endpoint.id,
            method=req.endpoint.method,
            path=req.endpoint.path,
            request_fields=[
                _hb.HALField(f.name, f.c_type, f.required, f.max_len, f.description)
                for f in req.endpoint.request_fields
            ],
            response_fields=[
                _hb.HALField(f.name, f.c_type, f.required, f.max_len, f.description)
                for f in req.endpoint.response_fields
            ],
            description=req.endpoint.description,
        )
        br = _hb.BindingRequest(
            nl_prompt=req.nl_prompt,
            endpoint=endpoint,
            server=req.server,
            use_llm=req.use_llm,
        )
        result = _hb.generate_binding(br)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "files": result.files,
        "server": result.server,
        "endpoint_id": result.endpoint_id,
        "llm_provider": result.llm_provider,
        "llm_used": result.llm_used,
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Components
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@router.get("/components", dependencies=[Depends(_require)])
async def get_components() -> dict[str, Any]:
    return _hc.summary()


@router.post("/components/assemble", dependencies=[Depends(_require)])
async def post_assemble(req: AssembleRequest) -> dict[str, Any]:
    try:
        return _hc.assemble_components(req.components)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
