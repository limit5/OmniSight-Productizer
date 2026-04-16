"""C26 — Shared HMI component library.

Component catalogue used by D-series skills (D2 IPCam, D8 Router,
D9 5G-GW, D17 Industrial-PC, D24 POS, D25 Kiosk). Each component is a
pair of (HTML fragment + matching JS initializer + backend HAL
endpoint spec) that the generator can splice into a page.

All components conform to the IEC 62443 baseline enforced by
``hmi_framework`` — no inline event handlers, no ``innerHTML``
assignment, no eval.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from backend import hmi_binding as _hb

logger = logging.getLogger(__name__)

COMPONENT_LIBRARY_VERSION = "1.0.0"


@dataclass
class Component:
    id: str
    title: str
    description: str
    required_endpoints: list[str] = field(default_factory=list)
    used_by_skills: list[str] = field(default_factory=list)

    def render_html(self) -> str:
        raise NotImplementedError

    def render_js(self) -> str:
        raise NotImplementedError

    def hal_endpoints(self) -> list[_hb.HALEndpoint]:
        raise NotImplementedError


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Network component
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class NetworkComponent(Component):
    def __init__(self) -> None:
        super().__init__(
            id="network",
            title="nav.network",
            description="DHCP/static IP, Wi-Fi SSID list, connectivity status",
            required_endpoints=["net_status", "net_apply"],
            used_by_skills=["D2", "D8", "D9", "D17", "D24", "D25"],
        )

    def render_html(self) -> str:
        return """\
<section id="c-network" data-component="network">
  <h2 data-i18n="nav.network">Network</h2>
  <div class="omni-network-status" data-bind="net_status"></div>
  <form data-bind-submit="net_apply">
    <label data-i18n="form.required">Mode
      <select name="mode">
        <option value="dhcp">DHCP</option>
        <option value="static">Static</option>
      </select>
    </label>
    <label>IP <input name="ip" type="text" pattern="[0-9.]+" maxlength="15"></label>
    <label>Netmask <input name="netmask" type="text" pattern="[0-9.]+" maxlength="15"></label>
    <label>Gateway <input name="gateway" type="text" pattern="[0-9.]+" maxlength="15"></label>
    <button type="submit" data-i18n="action.save">Save</button>
  </form>
</section>
"""

    def render_js(self) -> str:
        return """\
(function () {
  function refreshStatus() {
    if (!window.OmniHMI || !window.OmniHMI.clients || !window.OmniHMI.clients.net_status) return;
    window.OmniHMI.clients.net_status().then(function (data) {
      var el = document.querySelector('[data-bind="net_status"]');
      if (!el) return;
      el.textContent = (data && data.online ? "Online " : "Offline ") +
                       (data && data.ip ? data.ip : "");
    }).catch(function () { /* silent */ });
  }
  document.addEventListener("DOMContentLoaded", function () {
    var form = document.querySelector('[data-bind-submit="net_apply"]');
    if (form) {
      form.addEventListener("submit", function (ev) {
        ev.preventDefault();
        var payload = {};
        var data = new FormData(form);
        data.forEach(function (v, k) { payload[k] = v; });
        if (window.OmniHMI.clients.net_apply) {
          window.OmniHMI.clients.net_apply(payload).then(refreshStatus);
        }
      });
    }
    refreshStatus();
    setInterval(refreshStatus, 5000);
  });
})();
"""

    def hal_endpoints(self) -> list[_hb.HALEndpoint]:
        return [
            _hb.HALEndpoint(
                id="net_status",
                method="GET",
                path="/api/network/status",
                request_fields=[],
                response_fields=[
                    _hb.HALField("online", "bool"),
                    _hb.HALField("ip", "string", max_len=32),
                    _hb.HALField("mode", "string", max_len=16),
                ],
                description="Return current network link state and IP assignment",
            ),
            _hb.HALEndpoint(
                id="net_apply",
                method="POST",
                path="/api/network/apply",
                request_fields=[
                    _hb.HALField("mode", "string", max_len=16),
                    _hb.HALField("ip", "string", max_len=32),
                    _hb.HALField("netmask", "string", max_len=32),
                    _hb.HALField("gateway", "string", max_len=32),
                ],
                response_fields=[_hb.HALField("ok", "bool")],
                description="Apply network configuration (requires admin)",
            ),
        ]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  OTA component
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class OTAComponent(Component):
    def __init__(self) -> None:
        super().__init__(
            id="ota",
            title="nav.ota",
            description="Firmware upload, verify, apply, rollback",
            required_endpoints=["ota_status", "ota_upload", "ota_apply", "ota_rollback"],
            used_by_skills=["D2", "D8", "D9", "D17", "D24", "D25"],
        )

    def render_html(self) -> str:
        return """\
<section id="c-ota" data-component="ota">
  <h2 data-i18n="nav.ota">Firmware</h2>
  <div class="omni-ota-status" data-bind="ota_status"></div>
  <form data-bind-submit="ota_upload" enctype="multipart/form-data">
    <label data-i18n="form.required">Firmware image
      <input name="image" type="file" accept=".bin,.img,.fw">
    </label>
    <button type="submit" data-i18n="action.save">Upload</button>
  </form>
  <div class="omni-ota-actions">
    <button data-action="ota_apply" data-i18n="status.updating">Apply</button>
    <button data-action="ota_rollback" data-i18n="action.cancel">Rollback</button>
  </div>
</section>
"""

    def render_js(self) -> str:
        return """\
(function () {
  function attach(btnSelector, fn) {
    var btns = document.querySelectorAll(btnSelector);
    for (var i = 0; i < btns.length; i++) {
      btns[i].addEventListener("click", function (ev) {
        ev.preventDefault();
        fn();
      });
    }
  }
  function refresh() {
    if (!window.OmniHMI || !window.OmniHMI.clients || !window.OmniHMI.clients.ota_status) return;
    window.OmniHMI.clients.ota_status().then(function (data) {
      var el = document.querySelector('[data-bind="ota_status"]');
      if (!el) return;
      var label = (data && data.current_version) || "unknown";
      el.textContent = "Current firmware: " + label;
    }).catch(function () { /* silent */ });
  }
  document.addEventListener("DOMContentLoaded", function () {
    attach('[data-action="ota_apply"]', function () {
      if (window.OmniHMI.clients.ota_apply) { window.OmniHMI.clients.ota_apply({}).then(refresh); }
    });
    attach('[data-action="ota_rollback"]', function () {
      if (window.OmniHMI.clients.ota_rollback) { window.OmniHMI.clients.ota_rollback({}).then(refresh); }
    });
    refresh();
    setInterval(refresh, 10000);
  });
})();
"""

    def hal_endpoints(self) -> list[_hb.HALEndpoint]:
        return [
            _hb.HALEndpoint(
                id="ota_status",
                method="GET",
                path="/api/ota/status",
                response_fields=[
                    _hb.HALField("current_version", "string", max_len=64),
                    _hb.HALField("pending_version", "string", max_len=64),
                    _hb.HALField("progress_pct", "int"),
                ],
                description="Return current, pending firmware version + apply progress",
            ),
            _hb.HALEndpoint(
                id="ota_upload",
                method="POST",
                path="/api/ota/upload",
                response_fields=[_hb.HALField("ok", "bool"), _hb.HALField("sha256", "string", max_len=72)],
                description="Upload firmware image (multipart) and checksum",
            ),
            _hb.HALEndpoint(
                id="ota_apply",
                method="POST",
                path="/api/ota/apply",
                response_fields=[_hb.HALField("ok", "bool")],
                description="Apply uploaded firmware (requires admin)",
            ),
            _hb.HALEndpoint(
                id="ota_rollback",
                method="POST",
                path="/api/ota/rollback",
                response_fields=[_hb.HALField("ok", "bool")],
                description="Roll back to previous firmware partition",
            ),
        ]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Logs component
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class LogsComponent(Component):
    def __init__(self) -> None:
        super().__init__(
            id="logs",
            title="nav.logs",
            description="Tail / search / export device logs",
            required_endpoints=["logs_tail", "logs_export"],
            used_by_skills=["D2", "D8", "D9", "D17", "D24", "D25"],
        )

    def render_html(self) -> str:
        return """\
<section id="c-logs" data-component="logs">
  <h2 data-i18n="nav.logs">Logs</h2>
  <div class="omni-logs-filter">
    <input name="query" type="search" maxlength="128" placeholder="search">
    <select name="level">
      <option value="">all</option>
      <option value="error">error</option>
      <option value="warn">warn</option>
      <option value="info">info</option>
    </select>
    <button data-action="logs_refresh">Refresh</button>
    <button data-action="logs_export">Export</button>
  </div>
  <pre class="omni-logs-body" data-bind="logs_tail"></pre>
</section>
"""

    def render_js(self) -> str:
        return """\
(function () {
  function readFilter() {
    var root = document.querySelector('#c-logs');
    if (!root) return { query: "", level: "" };
    return {
      query: (root.querySelector('input[name="query"]') || {}).value || "",
      level: (root.querySelector('select[name="level"]') || {}).value || "",
    };
  }
  function refresh() {
    if (!window.OmniHMI || !window.OmniHMI.clients || !window.OmniHMI.clients.logs_tail) return;
    window.OmniHMI.clients.logs_tail(readFilter()).then(function (data) {
      var el = document.querySelector('[data-bind="logs_tail"]');
      if (!el || !data) return;
      var lines = Array.isArray(data.lines) ? data.lines : [];
      el.textContent = lines.join("\\n");
    }).catch(function () { /* silent */ });
  }
  function doExport() {
    if (!window.OmniHMI.clients.logs_export) return;
    window.OmniHMI.clients.logs_export(readFilter()).then(function (data) {
      if (data && data.url) { window.location.href = data.url; }
    });
  }
  document.addEventListener("DOMContentLoaded", function () {
    var btn = document.querySelector('[data-action="logs_refresh"]');
    if (btn) btn.addEventListener("click", function (e) { e.preventDefault(); refresh(); });
    var exp = document.querySelector('[data-action="logs_export"]');
    if (exp) exp.addEventListener("click", function (e) { e.preventDefault(); doExport(); });
    refresh();
    setInterval(refresh, 3000);
  });
})();
"""

    def hal_endpoints(self) -> list[_hb.HALEndpoint]:
        return [
            _hb.HALEndpoint(
                id="logs_tail",
                method="GET",
                path="/api/logs/tail",
                request_fields=[
                    _hb.HALField("query", "string", required=False, max_len=128),
                    _hb.HALField("level", "string", required=False, max_len=16),
                ],
                response_fields=[_hb.HALField("count", "int")],
                description="Return the last N log lines filtered by level/query",
            ),
            _hb.HALEndpoint(
                id="logs_export",
                method="POST",
                path="/api/logs/export",
                request_fields=[
                    _hb.HALField("query", "string", required=False, max_len=128),
                    _hb.HALField("level", "string", required=False, max_len=16),
                ],
                response_fields=[_hb.HALField("url", "string", max_len=256)],
                description="Generate a downloadable log archive",
            ),
        ]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Registry
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


_REGISTRY: dict[str, Component] = {
    "network": NetworkComponent(),
    "ota": OTAComponent(),
    "logs": LogsComponent(),
}


def list_components() -> list[Component]:
    return list(_REGISTRY.values())


def get_component(component_id: str) -> Component:
    if component_id not in _REGISTRY:
        raise KeyError(f"Unknown component '{component_id}'")
    return _REGISTRY[component_id]


def assemble_components(component_ids: list[str]) -> dict[str, Any]:
    """Bundle HTML + JS + HAL endpoints for a set of components."""
    html_parts: list[str] = []
    js_parts: list[str] = []
    endpoints: list[_hb.HALEndpoint] = []
    selected: list[str] = []
    for cid in component_ids:
        comp = get_component(cid)
        html_parts.append(comp.render_html())
        js_parts.append(comp.render_js())
        endpoints.extend(comp.hal_endpoints())
        selected.append(cid)
    return {
        "components": selected,
        "html": "\n".join(html_parts),
        "js": "\n".join(js_parts),
        "endpoints": [
            {
                "id": e.id,
                "method": e.method,
                "path": e.path,
                "request_fields": [f.name for f in e.request_fields],
                "response_fields": [f.name for f in e.response_fields],
                "description": e.description,
            }
            for e in endpoints
        ],
    }


def summary() -> dict[str, Any]:
    return {
        "library_version": COMPONENT_LIBRARY_VERSION,
        "components": [
            {
                "id": c.id,
                "title": c.title,
                "description": c.description,
                "used_by_skills": c.used_by_skills,
                "required_endpoints": c.required_endpoints,
            }
            for c in list_components()
        ],
    }
