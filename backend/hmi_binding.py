"""C26 — NL + HAL schema → C handler skeleton + JS client generator.

Given a natural-language prompt (intent) and a HAL schema (JSON
describing endpoints, request/response shapes), emit a pair of files:

    * ``<endpoint>_handler.c`` — fastcgi / mongoose / civetweb handler
      skeleton with input validation + JSON marshalling stubs
    * ``<endpoint>_client.js`` — matching async JS client wrapping
      ``OmniHMI.fetchJSON``

The NL prompt is routed through ``hmi_llm`` (pluggable provider) to
enrich the binding with docstrings and field descriptions; when the LLM
is unavailable the rule-based fallback still produces a correct
skeleton (minus prose).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from backend import hmi_llm as _llm

logger = logging.getLogger(__name__)

BINDING_VERSION = "1.0.0"

SUPPORTED_SERVERS = ("fastcgi", "mongoose", "civetweb")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Types
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass
class HALField:
    name: str
    c_type: str                    # "int" | "float" | "bool" | "string"
    required: bool = True
    max_len: int = 128             # strings only
    description: str = ""


@dataclass
class HALEndpoint:
    id: str
    method: str                    # "GET" | "POST" | "PUT" | "DELETE"
    path: str                      # "/api/network/wifi"
    request_fields: list[HALField] = field(default_factory=list)
    response_fields: list[HALField] = field(default_factory=list)
    description: str = ""


@dataclass
class BindingRequest:
    nl_prompt: str
    endpoint: HALEndpoint
    server: str = "mongoose"       # fastcgi | mongoose | civetweb
    use_llm: bool = True

    def __post_init__(self) -> None:
        if self.server not in SUPPORTED_SERVERS:
            raise ValueError(f"server must be one of {SUPPORTED_SERVERS}")
        _validate_identifier(self.endpoint.id, "endpoint.id")
        _validate_path(self.endpoint.path, "endpoint.path")
        if self.endpoint.method not in ("GET", "POST", "PUT", "DELETE"):
            raise ValueError(f"Invalid method: {self.endpoint.method}")
        for f in list(self.endpoint.request_fields) + list(self.endpoint.response_fields):
            _validate_identifier(f.name, f"field.name ({f.name})")
            if f.c_type not in ("int", "float", "bool", "string"):
                raise ValueError(f"Invalid c_type: {f.c_type}")


@dataclass
class BindingResult:
    files: dict[str, str]
    server: str
    endpoint_id: str
    llm_provider: str
    llm_used: bool


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Validation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


_IDENT_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]{0,63}$")
_PATH_RE = re.compile(r"^/[A-Za-z0-9_\-/:{}]*$")


def _validate_identifier(value: str, label: str) -> None:
    if not _IDENT_RE.match(value or ""):
        raise ValueError(f"Invalid {label}: must match {_IDENT_RE.pattern}")


def _validate_path(value: str, label: str) -> None:
    if not _PATH_RE.match(value or ""):
        raise ValueError(f"Invalid {label}: must match {_PATH_RE.pattern}")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  C handler generation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


_MONGOOSE_TEMPLATE = """\
/* C26 generated HMI binding: {endpoint_id}
 * Server: mongoose
 * {description}
 *
 * Wire this handler in mg_http_event_handler:
 *     if (mg_match(hm->uri, mg_str("{path}"), NULL)) {{
 *         handle_{endpoint_id}(c, hm);
 *         return;
 *     }}
 */
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include "mongoose.h"

typedef struct {{
{request_struct}
}} req_{endpoint_id}_t;

typedef struct {{
{response_struct}
}} resp_{endpoint_id}_t;

static int parse_{endpoint_id}(struct mg_http_message *hm, req_{endpoint_id}_t *req) {{
    (void)hm; (void)req;
{parse_body}
    return 0;
}}

static void render_{endpoint_id}(struct mg_connection *c, const resp_{endpoint_id}_t *resp) {{
    mg_http_reply(c, 200, "Content-Type: application/json\\r\\n"
                          "X-Content-Type-Options: nosniff\\r\\n",
                  "{{{json_render_format}}}",
{json_render_args});
}}

void handle_{endpoint_id}(struct mg_connection *c, struct mg_http_message *hm) {{
    if (!mg_vcasecmp(&hm->method, "{method}") == 0) {{
        mg_http_reply(c, 405, "", "Method not allowed\\n");
        return;
    }}
    req_{endpoint_id}_t req = {{0}};
    if (parse_{endpoint_id}(hm, &req) != 0) {{
        mg_http_reply(c, 400, "", "Invalid request\\n");
        return;
    }}
    resp_{endpoint_id}_t resp = {{0}};
    /* TODO: call into HAL to populate resp from req */
    render_{endpoint_id}(c, &resp);
}}
"""


_FASTCGI_TEMPLATE = """\
/* C26 generated HMI binding: {endpoint_id}
 * Server: fastcgi
 * {description}
 *
 * Register with FCGI_Accept loop via Nginx / lighttpd upstream.
 */
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <fcgi_stdio.h>

typedef struct {{
{request_struct}
}} req_{endpoint_id}_t;

typedef struct {{
{response_struct}
}} resp_{endpoint_id}_t;

static int parse_{endpoint_id}(const char *body, req_{endpoint_id}_t *req) {{
    (void)body; (void)req;
{parse_body}
    return 0;
}}

static void render_{endpoint_id}(const resp_{endpoint_id}_t *resp) {{
    FCGI_printf("Status: 200\\r\\n"
                "Content-Type: application/json\\r\\n"
                "X-Content-Type-Options: nosniff\\r\\n"
                "\\r\\n");
    FCGI_printf("{{{json_render_format}}}",
{json_render_args});
}}

int handle_{endpoint_id}(void) {{
    const char *method = getenv("REQUEST_METHOD");
    if (!method || strcmp(method, "{method}") != 0) {{
        FCGI_printf("Status: 405\\r\\n\\r\\n");
        return -1;
    }}
    char body[4096] = {{0}};
    fread(body, 1, sizeof(body) - 1, stdin);
    req_{endpoint_id}_t req = {{0}};
    if (parse_{endpoint_id}(body, &req) != 0) {{
        FCGI_printf("Status: 400\\r\\n\\r\\n");
        return -1;
    }}
    resp_{endpoint_id}_t resp = {{0}};
    /* TODO: call into HAL to populate resp */
    render_{endpoint_id}(&resp);
    return 0;
}}
"""


_CIVETWEB_TEMPLATE = """\
/* C26 generated HMI binding: {endpoint_id}
 * Server: civetweb
 * {description}
 *
 * Register with mg_set_request_handler(ctx, "{path}", handle_{endpoint_id}, NULL);
 */
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include "civetweb.h"

typedef struct {{
{request_struct}
}} req_{endpoint_id}_t;

typedef struct {{
{response_struct}
}} resp_{endpoint_id}_t;

static int parse_{endpoint_id}(const char *body, req_{endpoint_id}_t *req) {{
    (void)body; (void)req;
{parse_body}
    return 0;
}}

static void render_{endpoint_id}(struct mg_connection *conn, const resp_{endpoint_id}_t *resp) {{
    mg_printf(conn,
              "HTTP/1.1 200 OK\\r\\n"
              "Content-Type: application/json\\r\\n"
              "X-Content-Type-Options: nosniff\\r\\n\\r\\n");
    mg_printf(conn, "{{{json_render_format}}}",
{json_render_args});
}}

int handle_{endpoint_id}(struct mg_connection *conn, void *cbdata) {{
    (void)cbdata;
    const struct mg_request_info *ri = mg_get_request_info(conn);
    if (!ri || strcmp(ri->request_method, "{method}") != 0) {{
        mg_printf(conn, "HTTP/1.1 405 Method Not Allowed\\r\\n\\r\\n");
        return 405;
    }}
    char body[4096] = {{0}};
    mg_read(conn, body, sizeof(body) - 1);
    req_{endpoint_id}_t req = {{0}};
    if (parse_{endpoint_id}(body, &req) != 0) {{
        mg_printf(conn, "HTTP/1.1 400 Bad Request\\r\\n\\r\\n");
        return 400;
    }}
    resp_{endpoint_id}_t resp = {{0}};
    /* TODO: call into HAL to populate resp */
    render_{endpoint_id}(conn, &resp);
    return 200;
}}
"""


def _c_field(f: HALField) -> str:
    if f.c_type == "string":
        return f"    char {f.name}[{f.max_len}];"
    if f.c_type == "bool":
        return f"    int {f.name};   /* bool */"
    return f"    {f.c_type} {f.name};"


def _json_format(f: HALField) -> str:
    if f.c_type == "string":
        return f'\\"{f.name}\\": \\"%s\\"'
    if f.c_type == "bool":
        return f'\\"{f.name}\\": %s'
    if f.c_type == "int":
        return f'\\"{f.name}\\": %d'
    if f.c_type == "float":
        return f'\\"{f.name}\\": %f'
    return f'\\"{f.name}\\": null'


def _json_arg(f: HALField) -> str:
    if f.c_type == "bool":
        return f'resp->{f.name} ? "true" : "false"'
    return f"resp->{f.name}"


def _render_c_handler(req: BindingRequest) -> str:
    ep = req.endpoint
    req_struct = "\n".join(_c_field(f) for f in ep.request_fields) or "    int _placeholder;"
    resp_struct = "\n".join(_c_field(f) for f in ep.response_fields) or "    int _placeholder;"

    parse_lines: list[str] = []
    for f in ep.request_fields:
        parse_lines.append(f"    /* TODO: extract '{f.name}' ({f.c_type}) from request body */")
    parse_body = "\n".join(parse_lines) or "    /* no request fields */"

    resp_fields = ep.response_fields or []
    fmt_parts = [_json_format(f) for f in resp_fields] or ['\\"ok\\": true']
    arg_parts = [_json_arg(f) for f in resp_fields]
    render_args = "                  " + ",\n                  ".join(arg_parts) if arg_parts else "                  0"

    template_map = {
        "mongoose": _MONGOOSE_TEMPLATE,
        "fastcgi": _FASTCGI_TEMPLATE,
        "civetweb": _CIVETWEB_TEMPLATE,
    }
    tmpl = template_map[req.server]
    return tmpl.format(
        endpoint_id=ep.id,
        description=ep.description or "(no description)",
        path=ep.path,
        method=ep.method,
        request_struct=req_struct,
        response_struct=resp_struct,
        parse_body=parse_body,
        json_render_format=", ".join(fmt_parts),
        json_render_args=render_args,
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  JS client generation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _render_js_client(req: BindingRequest) -> str:
    ep = req.endpoint
    req_params = [f.name for f in ep.request_fields]
    req_arg = "payload" if req_params else ""
    request_shape = ", ".join(f"{f.name}: {f.c_type}" for f in ep.request_fields) or "(none)"
    response_shape = ", ".join(f"{f.name}: {f.c_type}" for f in ep.response_fields) or "ok: bool"

    if ep.method == "GET":
        body_part = "    var q = new URLSearchParams(payload || {}).toString();\n" \
                    f'    var url = "{ep.path}" + (q ? "?" + q : "");\n' \
                    '    return window.OmniHMI.fetchJSON(url, { method: "GET" });'
    else:
        body_part = (
            f'    return window.OmniHMI.fetchJSON("{ep.path}", {{\n'
            f'      method: "{ep.method}",\n'
            '      headers: { "Content-Type": "application/json" },\n'
            "      body: JSON.stringify(payload || {}),\n"
            "    });"
        )

    return f"""\
// C26 generated HMI client: {ep.id}
// Request shape: {request_shape}
// Response shape: {response_shape}
(function () {{
  window.OmniHMI = window.OmniHMI || {{}};
  window.OmniHMI.clients = window.OmniHMI.clients || {{}};
  window.OmniHMI.clients.{ep.id} = function ({req_arg}) {{
{body_part}
  }};
}})();
"""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Orchestration
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def generate_binding(req: BindingRequest) -> BindingResult:
    # Ask LLM for an enriched description if available — but the skeleton
    # is always deterministic regardless of the provider.
    llm_used = False
    llm_provider = "disabled"
    if req.use_llm:
        enriched = _llm.enrich_binding_description(req.nl_prompt, req.endpoint)
        llm_used = enriched.used_real_llm
        llm_provider = enriched.provider
        if enriched.description and not req.endpoint.description:
            req.endpoint.description = enriched.description

    c_handler = _render_c_handler(req)
    js_client = _render_js_client(req)

    return BindingResult(
        files={
            f"{req.endpoint.id}_handler.c": c_handler,
            f"{req.endpoint.id}_client.js": js_client,
        },
        server=req.server,
        endpoint_id=req.endpoint.id,
        llm_provider=llm_provider,
        llm_used=llm_used,
    )


def parse_hal_schema(schema: dict[str, Any]) -> HALEndpoint:
    """Build a :class:`HALEndpoint` from a JSON-serialisable dict."""
    def _fields(items: list[dict[str, Any]]) -> list[HALField]:
        out: list[HALField] = []
        for f in items:
            out.append(HALField(
                name=f["name"],
                c_type=f["c_type"],
                required=bool(f.get("required", True)),
                max_len=int(f.get("max_len", 128)),
                description=f.get("description", ""),
            ))
        return out
    return HALEndpoint(
        id=schema["id"],
        method=schema.get("method", "POST"),
        path=schema["path"],
        request_fields=_fields(schema.get("request_fields", [])),
        response_fields=_fields(schema.get("response_fields", [])),
        description=schema.get("description", ""),
    )


def summary() -> dict[str, Any]:
    return {
        "binding_version": BINDING_VERSION,
        "supported_servers": list(SUPPORTED_SERVERS),
    }
