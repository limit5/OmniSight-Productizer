"""C26 — HMI binding generator tests (#261).

Covers: NL + HAL schema → C handler + JS client for each supported
server (fastcgi / mongoose / civetweb), input validation on endpoint
id / path / method / c_type, parse_hal_schema round-trip, LLM
enrichment path (rule-based fallback always available).
"""

from __future__ import annotations

import os

import pytest

from backend import hmi_binding as b


def _wifi_endpoint() -> b.HALEndpoint:
    return b.HALEndpoint(
        id="wifi_connect",
        method="POST",
        path="/api/network/wifi",
        request_fields=[
            b.HALField("ssid", "string", max_len=64),
            b.HALField("password", "string", max_len=128),
        ],
        response_fields=[
            b.HALField("connected", "bool"),
            b.HALField("rssi", "int"),
        ],
    )


class TestCHandlerGeneration:
    @pytest.mark.parametrize("server", ["fastcgi", "mongoose", "civetweb"])
    def test_generates_handler_and_client(self, server):
        req = b.BindingRequest(
            nl_prompt="connect to wifi",
            endpoint=_wifi_endpoint(),
            server=server,
            use_llm=False,
        )
        res = b.generate_binding(req)
        assert f"{server}" == res.server
        assert "wifi_connect_handler.c" in res.files
        assert "wifi_connect_client.js" in res.files
        c = res.files["wifi_connect_handler.c"]
        assert "handle_wifi_connect" in c
        # Struct fields
        assert "char ssid[64]" in c
        assert "char password[128]" in c
        assert "connected" in c
        js = res.files["wifi_connect_client.js"]
        assert "OmniHMI.clients.wifi_connect" in js
        assert "/api/network/wifi" in js

    def test_get_method_generates_querystring_client(self):
        ep = b.HALEndpoint(id="net_status", method="GET", path="/api/network/status")
        req = b.BindingRequest(nl_prompt="get network status", endpoint=ep, use_llm=False)
        res = b.generate_binding(req)
        js = res.files["net_status_client.js"]
        assert "URLSearchParams" in js
        assert '"GET"' in js

    def test_post_method_generates_json_body_client(self):
        ep = b.HALEndpoint(
            id="reboot",
            method="POST",
            path="/api/system/reboot",
            request_fields=[b.HALField("delay_s", "int")],
        )
        req = b.BindingRequest(nl_prompt="reboot", endpoint=ep, use_llm=False)
        res = b.generate_binding(req)
        js = res.files["reboot_client.js"]
        assert "JSON.stringify(payload" in js
        assert '"POST"' in js

    def test_empty_fields_uses_placeholder(self):
        ep = b.HALEndpoint(id="ping", method="GET", path="/api/ping")
        req = b.BindingRequest(nl_prompt="ping", endpoint=ep, use_llm=False)
        res = b.generate_binding(req)
        c = res.files["ping_handler.c"]
        assert "_placeholder" in c or "ok" in c


class TestValidation:
    def test_bad_endpoint_id(self):
        ep = b.HALEndpoint(id="9bad", method="POST", path="/api/x")
        with pytest.raises(ValueError):
            b.BindingRequest(nl_prompt="", endpoint=ep)

    def test_bad_path(self):
        ep = b.HALEndpoint(id="ok", method="POST", path="no-slash")
        with pytest.raises(ValueError):
            b.BindingRequest(nl_prompt="", endpoint=ep)

    def test_bad_method(self):
        ep = b.HALEndpoint(id="ok", method="TRACE", path="/api/x")
        with pytest.raises(ValueError):
            b.BindingRequest(nl_prompt="", endpoint=ep)

    def test_bad_c_type(self):
        ep = b.HALEndpoint(
            id="ok",
            method="POST",
            path="/api/x",
            request_fields=[b.HALField("f", "mystery")],
        )
        with pytest.raises(ValueError):
            b.BindingRequest(nl_prompt="", endpoint=ep)

    def test_bad_server(self):
        ep = b.HALEndpoint(id="ok", method="POST", path="/api/x")
        with pytest.raises(ValueError):
            b.BindingRequest(nl_prompt="", endpoint=ep, server="nginx")


class TestHALSchemaParse:
    def test_round_trip(self):
        schema = {
            "id": "logs_tail",
            "method": "GET",
            "path": "/api/logs/tail",
            "request_fields": [
                {"name": "query", "c_type": "string", "max_len": 256},
                {"name": "level", "c_type": "string", "required": False},
            ],
            "response_fields": [{"name": "count", "c_type": "int"}],
            "description": "tail",
        }
        ep = b.parse_hal_schema(schema)
        assert ep.id == "logs_tail"
        assert ep.method == "GET"
        assert len(ep.request_fields) == 2
        assert ep.request_fields[0].max_len == 256
        assert ep.request_fields[1].required is False


class TestLLMIntegration:
    def test_rule_based_is_default_in_tests(self, monkeypatch):
        # Neither anthropic key nor ollama host — must fall back
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("HMI_LLM_PROVIDER", raising=False)
        monkeypatch.delenv("OMNISIGHT_LLM_PROVIDER", raising=False)
        ep = _wifi_endpoint()
        req = b.BindingRequest(nl_prompt="connect wifi", endpoint=ep, use_llm=True)
        res = b.generate_binding(req)
        assert res.llm_provider == "rule_based"
        assert res.llm_used is False

    def test_description_not_overwritten_when_explicit(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        ep = _wifi_endpoint()
        ep.description = "User-specified description"
        req = b.BindingRequest(nl_prompt="wifi", endpoint=ep, use_llm=True)
        res = b.generate_binding(req)
        assert "User-specified" in res.files["wifi_connect_handler.c"]


class TestSummary:
    def test_summary_shape(self):
        s = b.summary()
        assert s["binding_version"] == b.BINDING_VERSION
        assert set(s["supported_servers"]) == {"fastcgi", "mongoose", "civetweb"}
