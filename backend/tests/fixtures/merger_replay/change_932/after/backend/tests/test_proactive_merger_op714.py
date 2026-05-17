class TestProactiveMergerOP714:
    async def test_logs_arbiter_and_underlying_merger_reason(self, caplog, merger_http_env):
        mock_client = MagicMock()
        from backend.agents.conflict_enrichment import ConflictFile, EnrichmentResult
        with patch("backend.gerrit.gerrit_client", mock_client), patch("backend.agents.conflict_enrichment.enrich_via_local_merge"):
            await _proactive_merger_check(_event(change_number=1421, ps_number=3))
        assert any("merger_reason=refused_no_conflict" in r.message for r in caplog.records)

    async def test_drift_verify_red_does_not_call_caller_push(self, caplog, merger_http_env):
        mock_client = MagicMock()
        from backend.agents.conflict_enrichment import ConflictFile, EnrichmentResult
        with patch("backend.gerrit.gerrit_client", mock_client), patch("backend.routers.webhooks._daemon_apply_resolution_push"):
            await _proactive_merger_check(_event())
        assert any("verify_result=red" in r.message for r in caplog.records)
