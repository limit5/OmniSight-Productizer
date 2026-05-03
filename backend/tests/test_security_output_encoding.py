"""SC.7.2 — Unit tests for OWASP output-encoding helpers."""

from __future__ import annotations

import json

from backend.security import output_encoding as oe


class TestEncodeHtmlText:
    def test_escapes_html_control_characters_for_text_nodes(self):
        assert oe.encode_html_text("<b>Alice & Bob</b>") == "&lt;b&gt;Alice &amp; Bob&lt;/b&gt;"

    def test_preserves_quotes_in_text_nodes(self):
        assert oe.encode_html_text('"hello" and \'hi\'') == '"hello" and \'hi\''

    def test_none_becomes_empty_text(self):
        assert oe.encode_html_text(None) == ""


class TestEncodeHtmlAttribute:
    def test_escapes_quotes_for_quoted_attribute_values(self):
        value = '" onmouseover="alert(1)'
        assert oe.encode_html_attribute(value) == "&quot; onmouseover=&quot;alert(1)"

    def test_escapes_apostrophes_for_single_quoted_attributes(self):
        assert oe.encode_html_attribute("Bob's <tag>") == "Bob&#x27;s &lt;tag&gt;"


class TestEncodeJavascriptString:
    def test_returns_quoted_javascript_string_literal(self):
        assert oe.encode_javascript_string('Alice "Ops"') == '"Alice \\"Ops\\""'

    def test_breaks_closing_script_tag_and_html_sensitive_bytes(self):
        encoded = oe.encode_javascript_string("</script><img src=x onerror=alert(1)&")
        assert encoded == '"\\u003c/script\\u003e\\u003cimg src=x onerror=alert(1)\\u0026"'
        assert "</script>" not in encoded.lower()
        assert "<" not in encoded
        assert "&" not in encoded

    def test_escapes_javascript_line_separators(self):
        encoded = oe.encode_javascript_string("line\u2028next\u2029end")
        assert encoded == '"line\\u2028next\\u2029end"'


class TestEncodeJsonScript:
    def test_compacts_json_and_breaks_script_terminator(self):
        encoded = oe.encode_json_script({"name": "</script>", "enabled": True})
        assert encoded == '{"name":"\\u003c/script\\u003e","enabled":true}'
        assert "</script>" not in encoded.lower()
        assert json.loads(encoded) == {"name": "</script>", "enabled": True}

    def test_escapes_nested_html_sensitive_values(self):
        encoded = oe.encode_json_script({"items": ["<one>", "two & three"]})
        assert "\\u003cone\\u003e" in encoded
        assert "\\u0026" in encoded
        assert json.loads(encoded) == {"items": ["<one>", "two & three"]}


class TestEncodeUrlComponent:
    def test_percent_encodes_reserved_url_separators(self):
        assert oe.encode_url_component("tenant/a?role=admin&ok=1") == "tenant%2Fa%3Frole%3Dadmin%26ok%3D1"

    def test_percent_encodes_spaces_as_component_octets(self):
        assert oe.encode_url_component("Alice Ops") == "Alice%20Ops"
