"""U4.4 OP-2306 — fleet device-registry router tests.

Asserts the GET /fleet/devices, /fleet/devices/{id} and
/fleet/devices/{id}/manifest endpoints serve the bundled fixtures
under ``configs/fleet_devices/`` and 404 on unknown ids. Schema-shape
checks confirm each manifest carries the apps.manifest envelope
(schema_version=1, device.{id,display,default_renderer}, apps[]).

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
