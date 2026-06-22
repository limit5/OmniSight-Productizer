"""U4.4 OP-2306 / U4.6 OP-2308 — fleet device-registry router tests.

Asserts the GET /fleet/devices, /fleet/devices/{id} and
/fleet/devices/{id}/manifest endpoints serve the bundled fixtures
under ``configs/fleet_devices/`` and 404 on unknown ids. Schema-shape
checks confirm each manifest carries the apps.manifest envelope
(schema_version=1, device.{id,display,default_renderer}, apps[]).

OP-2308 adds POST /fleet/devices/{d}/apps/{a}/launch — a fixture-
scoped command stub. The tests below pin its acceptance contract
(safe target ⇒ ``dispatched`` ack; qt-only / unsafe target ⇒ 422;
unknown device or app ⇒ 404) without ever running a real remote
command.

Scope: only this file's tests run via
``pytest backend/tests/test_fleet_devices_router.py -v`` —
do NOT run the full suite (times out per the ticket DoD).
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import auth as _au
from backend.routers.fleet_devices import router as fleet_devices_router

KNOWN_DEVICE_IDS = {
    "pos-kiosk-rk3588",
    "ipcam-rv1126",
    "conference-appliance-rk3588",
}


def _anon_admin() -> _au.User:
    return _au.User(
        id="user-test-admin", email="admin@test.local", name="admin",
        role="admin", enabled=True,
    )


@pytest.fixture()
def client() -> TestClient:
    app = FastAPI()
    app.dependency_overrides[_au.current_user] = _anon_admin
    app.include_router(fleet_devices_router)
    return TestClient(app)


class TestListDevices:
    def test_list_returns_known_devices(self, client: TestClient) -> None:
        resp = client.get("/fleet/devices")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert isinstance(data, list)
        ids = {d["id"] for d in data}
        assert KNOWN_DEVICE_IDS.issubset(ids), ids

    def test_list_rows_carry_metadata(self, client: TestClient) -> None:
        rows = client.get("/fleet/devices").json()
        for row in rows:
            assert set(row.keys()) >= {"id", "name", "model", "renderer", "manifest_ref"}
            assert isinstance(row["id"], str) and row["id"]
            assert isinstance(row["name"], str) and row["name"]
            assert isinstance(row["model"], str) and row["model"]
            assert row["renderer"] in {"qt", "web"}
            assert isinstance(row["manifest_ref"], str) and row["manifest_ref"]


class TestGetDevice:
    @pytest.mark.parametrize("device_id", sorted(KNOWN_DEVICE_IDS))
    def test_known_device_returns_fixture(self, client: TestClient, device_id: str) -> None:
        resp = client.get(f"/fleet/devices/{device_id}")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["id"] == device_id
        assert body["model"]
        assert body["renderer"] in {"qt", "web"}
        # manifest block is NOT inlined on the metadata endpoint.
        assert "manifest" not in body

    def test_unknown_device_returns_404(self, client: TestClient) -> None:
        resp = client.get("/fleet/devices/does-not-exist")
        assert resp.status_code == 404
        assert "does-not-exist" in resp.json()["detail"]


class TestGetDeviceManifest:
    @pytest.mark.parametrize("device_id", sorted(KNOWN_DEVICE_IDS))
    def test_manifest_is_schema_shaped(
        self, client: TestClient, device_id: str
    ) -> None:
        resp = client.get(f"/fleet/devices/{device_id}/manifest")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # apps.manifest.schema.json: required top-level keys.
        assert body["schema_version"] == 1
        assert isinstance(body["device"], dict)
        assert isinstance(body["apps"], list) and body["apps"]
        # device block carries the id+display+default_renderer triple.
        dev = body["device"]
        assert dev["id"] == device_id
        assert dev["display"] in {"local", "headless", "both"}
        assert dev["default_renderer"] in {"qt", "web"}
        # Each app tile satisfies the schema's required keys.
        for app in body["apps"]:
            assert set(app.keys()) >= {"id", "title", "icon", "category", "entry"}
            assert isinstance(app["title"], dict) and "en" in app["title"]
            assert isinstance(app["entry"], dict) and app["entry"]

    def test_headless_device_renders_web(self, client: TestClient) -> None:
        body = client.get("/fleet/devices/ipcam-rv1126/manifest").json()
        # Schema allOf clause: display=headless implies default_renderer=web.
        assert body["device"]["display"] == "headless"
        assert body["device"]["default_renderer"] == "web"

    def test_unknown_device_manifest_returns_404(self, client: TestClient) -> None:
        resp = client.get("/fleet/devices/does-not-exist/manifest")
        assert resp.status_code == 404


class TestLaunchDeviceApp:
    """OP-2308 U4.6 — POST /fleet/devices/{d}/apps/{a}/launch stub.

    The endpoint never executes anything against a real remote device
    (live HIL is a deferred tier:X follow-up). It validates the
    device + app + the app's `entry.web` target against the same
    SAFE_PATH / http(s) policy launcher-web enforces on-device and
    returns a `dispatched` ack. Rejection paths are exercised below.
    """

    def test_launches_an_internal_target_for_a_web_routable_app(
        self, client: TestClient
    ) -> None:
        # ipcam-rv1126 / live-view has entry.web = "/live"
        resp = client.post("/fleet/devices/ipcam-rv1126/apps/live-view/launch")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["device_id"] == "ipcam-rv1126"
        assert body["app_id"] == "live-view"
        assert body["target"] == "/live"
        assert body["mode"] == "internal"
        assert body["status"] == "dispatched"
        assert body["dispatched_at"]  # ISO-8601 timestamp

    def test_launches_the_only_web_routable_pos_tile(
        self, client: TestClient
    ) -> None:
        # pos-kiosk-rk3588 / cashier has entry.web = "/cashier" (the
        # camera + factory-test apps are qt-only / process-only and
        # would 422 here — see test_qt_only_app_returns_422).
        resp = client.post(
            "/fleet/devices/pos-kiosk-rk3588/apps/cashier/launch"
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["device_id"] == "pos-kiosk-rk3588"
        assert body["app_id"] == "cashier"
        assert body["target"] == "/cashier"
        assert body["mode"] == "internal"

    def test_qt_only_app_returns_422_no_web_entry(
        self, client: TestClient
    ) -> None:
        # camera tile on pos-kiosk-rk3588 has only entry.qml — no
        # web/route target, so it is NOT launchable from the web mirror.
        resp = client.post(
            "/fleet/devices/pos-kiosk-rk3588/apps/camera/launch"
        )
        assert resp.status_code == 422
        assert "no-web-entry" in resp.json()["detail"]

    def test_process_only_app_returns_422_no_web_entry(
        self, client: TestClient
    ) -> None:
        # factory-test has only entry.process — same rejection.
        resp = client.post(
            "/fleet/devices/pos-kiosk-rk3588/apps/factory-test/launch"
        )
        assert resp.status_code == 422
        assert "no-web-entry" in resp.json()["detail"]

    def test_unknown_app_returns_404(self, client: TestClient) -> None:
        resp = client.post(
            "/fleet/devices/ipcam-rv1126/apps/does-not-exist/launch"
        )
        assert resp.status_code == 404
        assert "does-not-exist" in resp.json()["detail"]

    def test_unknown_device_returns_404(self, client: TestClient) -> None:
        resp = client.post("/fleet/devices/does-not-exist/apps/anything/launch")
        assert resp.status_code == 404

    def test_classify_target_rejects_unsafe_paths(self) -> None:
        """Direct coverage of the SAFE_PATH / scheme gate.

        The fixtures only carry safe targets, so this exercises the
        rejection branches without needing a malicious fixture. Mirrors
        the launcher-web `classifyTarget` policy.
        """
        from fastapi import HTTPException

        from backend.routers.fleet_devices import _classify_target

        for bad in (
            "",  # empty
            "javascript:alert(1)",  # bare scheme-less / non-URL
            "//evil.example/x",  # protocol-relative
            "/x?javascript:alert(1)",  # path with disallowed chars
            "data:text/html,<script>alert(1)</script>",  # data: scheme
            "ftp://x.example",  # non-http(s) scheme
        ):
            try:
                _classify_target(bad)
            except HTTPException as exc:
                assert exc.status_code == 422
                assert "unsafe-target" in exc.detail
            else:
                raise AssertionError(f"_classify_target accepted unsafe: {bad!r}")

    def test_classify_target_accepts_safe_internal_and_external(self) -> None:
        from backend.routers.fleet_devices import _classify_target

        assert _classify_target("/live") == ("internal", "/live")
        assert _classify_target("/admin/devices") == ("internal", "/admin/devices")
        mode, target = _classify_target("https://device.example/admin")
        assert mode == "external"
        assert target == "https://device.example/admin"
        mode, target = _classify_target("http://x.example")
        assert mode == "external"
