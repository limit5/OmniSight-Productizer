"""C26 — HMI router integration tests (#261).

Covers every /hmi/* endpoint: summary, platforms, ABI matrix + check,
locales + catalog, frameworks, generate (happy + 400), budget-check,
security-scan, binding/generate, components list + assemble. Uses
FastAPI TestClient with dependency overrides to bypass auth.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import auth as _au
from backend import hmi_framework as hf
from backend.routers.hmi import router as hmi_router


def _make_user(role: str = "operator") -> _au.User:
    return _au.User(
        id=f"user-{role}", email=f"{role}@test.local", name=role,
        role=role, tenant_id="tenantA",
    )


@pytest.fixture()
def client():
    hf.reload_config()
    app = FastAPI()
    app.dependency_overrides[_au.current_user] = lambda: _make_user("operator")
    app.dependency_overrides[_au.require_operator] = lambda: _make_user("operator")
    app.include_router(hmi_router)
    yield TestClient(app)


class TestSummaryRoutes:
    def test_summary(self, client):
        r = client.get("/hmi/summary")
        assert r.status_code == 200
        data = r.json()
        assert "framework" in data
        assert "generator" in data
        assert "binding" in data
        assert "components" in data
        assert "llm" in data

    def test_platforms(self, client):
        r = client.get("/hmi/platforms")
        assert r.status_code == 200
        names = {p["platform"] for p in r.json()}
        assert {"aarch64", "armv7", "riscv64", "host_native"}.issubset(names)

    def test_abi_matrix(self, client):
        r = client.get("/hmi/abi-matrix")
        assert r.status_code == 200
        data = r.json()
        assert "aarch64" in data
        assert any(e["engine"] == "chromium" for e in data["aarch64"])

    def test_abi_check_pass(self, client):
        r = client.post("/hmi/abi-check", json={
            "platform": "aarch64", "needs": {"wasm": True}, "needs_es_version": "ES2020",
        })
        assert r.status_code == 200
        assert r.json()["status"] == "pass"

    def test_abi_check_webrtc_requirement(self, client):
        r = client.post("/hmi/abi-check", json={
            "platform": "aarch64", "needs": {"webrtc": True}, "needs_es_version": "ES2020",
        })
        data = r.json()
        # WebKit lacks WebRTC on aarch64 — must appear in incompatible
        engines = {e["engine"] for e in data["incompatible"]}
        assert "webkit" in engines

    def test_locales(self, client):
        r = client.get("/hmi/locales")
        assert r.status_code == 200
        d = r.json()
        assert d["default"] == "en"
        codes = {loc["code"] for loc in d["supported"]}
        assert codes == {"en", "zh-TW", "ja", "zh-CN"}

    def test_catalog_populated(self, client):
        r = client.get("/hmi/i18n-catalog")
        d = r.json()
        assert "nav.home" in d["en"]
        assert d["zh-TW"]["action.save"]

    def test_frameworks(self, client):
        r = client.get("/hmi/frameworks")
        d = r.json()
        allowed = {f["name"] for f in d["allowed"]}
        assert {"preact", "lit-html", "vanilla"}.issubset(allowed)
        assert "react" in d["forbidden"]


class TestGenerate:
    def test_generate_happy_path(self, client):
        r = client.post("/hmi/generate", json={
            "product_name": "TestCam",
            "framework": "preact",
            "platform": "aarch64",
            "locale": "en",
            "sections": [{"id": "sec1", "title": "nav.network", "kind": "form"}],
        })
        assert r.status_code == 200
        d = r.json()
        assert "index.html" in d["files"]
        assert "app.js" in d["files"]
        assert d["security_status"] == "pass"
        assert d["total_bytes"] < d["budget_bytes"]

    def test_generate_rejects_forbidden_framework(self, client):
        r = client.post("/hmi/generate", json={
            "product_name": "X",
            "framework": "react",
            "platform": "aarch64",
            "sections": [],
        })
        assert r.status_code == 400
        assert "whitelist" in r.json()["detail"]

    def test_generate_rejects_eval_in_extra_scripts(self, client):
        r = client.post("/hmi/generate", json={
            "product_name": "X",
            "framework": "vanilla",
            "platform": "aarch64",
            "sections": [],
            "extra_scripts": 'eval("bad")',
        })
        assert r.status_code == 400


class TestBudgetCheck:
    def test_pass_within_budget(self, client):
        r = client.post("/hmi/budget-check", json={
            "platform": "aarch64",
            "files": {"index.html": "<p>ok</p>"},
        })
        assert r.status_code == 200
        assert r.json()["status"] == "pass"

    def test_fail_over_budget(self, client):
        payload = {"platform": "armv7", "files": {"big.js": "x" * (200 * 1024)}}
        r = client.post("/hmi/budget-check", json=payload)
        assert r.status_code == 200
        assert r.json()["status"] == "fail"
        assert r.json()["violations"]

    def test_unknown_platform_404(self, client):
        r = client.post("/hmi/budget-check", json={
            "platform": "mars64", "files": {},
        })
        assert r.status_code == 404


class TestSecurityScan:
    def test_clean_html(self, client):
        r = client.post("/hmi/security-scan", json={
            "html": "<p>safe</p>",
            "js": "let x = 1",
            "headers": {
                "Content-Security-Policy": (
                    "default-src 'self'; script-src 'self'; object-src 'none'; "
                    "base-uri 'none'; frame-ancestors 'none'"
                ),
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY",
                "Referrer-Policy": "no-referrer",
                "Strict-Transport-Security": "max-age=31536000",
            },
        })
        assert r.status_code == 200
        assert r.json()["status"] == "pass"

    def test_detects_eval(self, client):
        r = client.post("/hmi/security-scan", json={
            "html": "<p>x</p>",
            "js": 'eval("bad")',
            "headers": {},
        })
        data = r.json()
        assert data["status"] == "fail"


class TestBindingRouter:
    def test_binding_generates_files(self, client):
        r = client.post("/hmi/binding/generate", json={
            "nl_prompt": "connect wifi",
            "endpoint": {
                "id": "wifi_connect",
                "method": "POST",
                "path": "/api/network/wifi",
                "request_fields": [
                    {"name": "ssid", "c_type": "string", "max_len": 64},
                ],
                "response_fields": [
                    {"name": "connected", "c_type": "bool"},
                ],
            },
            "server": "mongoose",
            "use_llm": False,
        })
        assert r.status_code == 200
        d = r.json()
        assert "wifi_connect_handler.c" in d["files"]
        assert "wifi_connect_client.js" in d["files"]
        assert d["server"] == "mongoose"

    def test_binding_rejects_bad_method(self, client):
        r = client.post("/hmi/binding/generate", json={
            "nl_prompt": "",
            "endpoint": {"id": "ok", "method": "TRACE", "path": "/api/x"},
            "server": "mongoose",
        })
        assert r.status_code == 400


class TestComponents:
    def test_components_listing(self, client):
        r = client.get("/hmi/components")
        assert r.status_code == 200
        ids = {c["id"] for c in r.json()["components"]}
        assert ids == {"network", "ota", "logs"}

    def test_assemble_network_only(self, client):
        r = client.post("/hmi/components/assemble", json={"components": ["network"]})
        assert r.status_code == 200
        d = r.json()
        assert d["components"] == ["network"]
        assert len(d["endpoints"]) == 2

    def test_assemble_unknown_404(self, client):
        r = client.post("/hmi/components/assemble", json={"components": ["bogus"]})
        assert r.status_code == 404
