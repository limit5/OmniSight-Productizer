"""Tests for NPI lifecycle endpoints and artifact path traversal guard."""

import uuid

import pytest


class TestNPIEndpoints:

    @pytest.mark.asyncio
    async def test_get_npi_state(self, client):
        resp = await client.get("/api/v1/runtime/npi")
        assert resp.status_code == 200
        data = resp.json()
        assert "business_model" in data
        assert "phases" in data

    @pytest.mark.asyncio
    async def test_update_business_model(self, client):
        # Get initial state
        resp = await client.get("/api/v1/runtime/npi")
        assert resp.status_code == 200
        # Update to OBM
        resp = await client.put("/api/v1/runtime/npi", params={"business_model": "obm"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["business_model"] == "obm"
        # Restore
        await client.put("/api/v1/runtime/npi", params={"business_model": "odm"})

    @pytest.mark.asyncio
    async def test_phase_status_validation(self, client):
        resp = await client.get("/api/v1/runtime/npi")
        phases = resp.json().get("phases", [])
        if not phases:
            pytest.skip("No NPI phases loaded")
        phase_id = phases[0]["id"]
        # Valid status
        resp = await client.patch(f"/api/v1/runtime/npi/phases/{phase_id}", params={"status": "active"})
        assert resp.status_code == 200
        # Invalid status
        resp = await client.patch(f"/api/v1/runtime/npi/phases/{phase_id}", params={"status": "invalid_status"})
        assert resp.status_code == 400
        # Restore
        await client.patch(f"/api/v1/runtime/npi/phases/{phase_id}", params={"status": "pending"})

    @pytest.mark.asyncio
    async def test_milestone_status_validation(self, client):
        resp = await client.get("/api/v1/runtime/npi")
        phases = resp.json().get("phases", [])
        if not phases:
            pytest.skip("No NPI phases loaded")
        milestones = phases[0].get("milestones", [])
        if not milestones:
            pytest.skip("No milestones in first phase")
        ms_id = milestones[0]["id"]
        # Valid status
        resp = await client.patch(f"/api/v1/runtime/npi/milestones/{ms_id}", params={"status": "in_progress"})
        assert resp.status_code == 200
        # Invalid status
        resp = await client.patch(f"/api/v1/runtime/npi/milestones/{ms_id}", params={"status": "bogus"})
        assert resp.status_code == 400
        # Restore
        await client.patch(f"/api/v1/runtime/npi/milestones/{ms_id}", params={"status": "pending"})

    @pytest.mark.asyncio
    async def test_phase_auto_compute_pending(self, client):
        """When all milestones are pending, phase should be pending."""
        resp = await client.get("/api/v1/runtime/npi")
        phases = resp.json().get("phases", [])
        if not phases:
            pytest.skip("No NPI phases loaded")
        milestones = phases[0].get("milestones", [])
        if not milestones:
            pytest.skip("No milestones in first phase")
        ms_id = milestones[0]["id"]
        # Set to in_progress then back to pending
        await client.patch(f"/api/v1/runtime/npi/milestones/{ms_id}", params={"status": "in_progress"})
        resp = await client.patch(f"/api/v1/runtime/npi/milestones/{ms_id}", params={"status": "pending"})
        assert resp.status_code == 200
        # Check phase status
        state = (await client.get("/api/v1/runtime/npi")).json()
        phase = next(p for p in state["phases"] if p["id"] == phases[0]["id"])
        # If all milestones are pending, phase should be pending
        all_pending = all(m["status"] == "pending" for m in phase["milestones"])
        if all_pending:
            assert phase["status"] == "pending"

    @pytest.mark.asyncio
    async def test_milestone_not_found(self, client):
        resp = await client.patch("/api/v1/runtime/npi/milestones/nonexistent-id", params={"status": "completed"})
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_phase_not_found(self, client):
        resp = await client.patch("/api/v1/runtime/npi/phases/nonexistent-id", params={"status": "active"})
        assert resp.status_code == 404


class TestArtifactPathTraversal:

    @pytest.mark.asyncio
    async def test_download_nonexistent_artifact(self, client):
        resp = await client.get("/api/v1/artifacts/nonexistent-id/download")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_path_traversal_blocked(self, client):
        """Artifact with path outside .artifacts/ should be rejected."""
        from backend import db
        art_id = f"art-traversal-{uuid.uuid4().hex[:6]}"
        # Insert artifact with a traversal path
        await db.insert_artifact({
            "id": art_id, "task_id": "t-test", "agent_id": "a1",
            "name": "evil.md", "type": "markdown",
            "file_path": "/etc/passwd",
            "size": 100, "created_at": "2026-01-01T00:00:00",
        })
        resp = await client.get(f"/api/v1/artifacts/{art_id}/download")
        assert resp.status_code == 403
