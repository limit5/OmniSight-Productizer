"""O10 (#273) — Unit tests for backend.security_hardening.

Covers each sub-component in isolation so a regression in one
(HMAC envelope, Redis ACL rendering, worker attestation, merger vote
chain, Gerrit permission verifier) fails exactly one test class and
doesn't blast the matrix.
"""

from __future__ import annotations

import dataclasses
import time

import pytest

from backend import security_hardening as sh


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  1. Queue HMAC envelope
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestQueueHmac:
    KEY = sh.QueueHmacKey(key_id="k1", secret=b"supersecret-key-value-16+")

    def test_sign_then_verify_roundtrips_payload(self):
        payload = {"task_id": "T-1", "priority": "P0", "body": {"x": [1, 2]}}
        signed = sh.sign_envelope(payload, self.KEY)
        verified = sh.verify_envelope(signed, self.KEY)
        assert verified == payload

    def test_sign_does_not_mutate_input(self):
        payload = {"task_id": "T-1"}
        original = dict(payload)
        sh.sign_envelope(payload, self.KEY)
        assert payload == original
        assert sh.HMAC_HEADER_FIELD not in payload

    def test_payload_tamper_rejected(self):
        signed = sh.sign_envelope({"task_id": "T-1"}, self.KEY)
        tampered = dict(signed)
        tampered["task_id"] = "T-2"
        with pytest.raises(sh.HmacVerifyError, match="signature mismatch"):
            sh.verify_envelope(tampered, self.KEY)

    def test_wrong_key_rejected(self):
        signed = sh.sign_envelope({"x": 1}, self.KEY)
        other = sh.QueueHmacKey(key_id="k1", secret=b"different-secret")
        with pytest.raises(sh.HmacVerifyError, match="signature mismatch"):
            sh.verify_envelope(signed, other)

    def test_wrong_key_id_rejected(self):
        signed = sh.sign_envelope({"x": 1}, self.KEY)
        other = sh.QueueHmacKey(key_id="k2", secret=self.KEY.secret)
        with pytest.raises(sh.HmacVerifyError, match="key id mismatch"):
            sh.verify_envelope(signed, other)

    def test_expired_envelope_rejected(self):
        signed = sh.sign_envelope({"x": 1}, self.KEY, ts=time.time() - 3600)
        with pytest.raises(sh.HmacVerifyError, match="too old"):
            sh.verify_envelope(signed, self.KEY, max_age_s=60)

    def test_future_timestamp_rejected(self):
        signed = sh.sign_envelope({"x": 1}, self.KEY, ts=time.time() + 3600)
        with pytest.raises(sh.HmacVerifyError, match="future"):
            sh.verify_envelope(signed, self.KEY, max_age_s=60)

    def test_missing_envelope_fields_rejected(self):
        with pytest.raises(sh.HmacVerifyError, match="missing"):
            sh.verify_envelope({"x": 1}, self.KEY)

    def test_malformed_header_rejected(self):
        signed = sh.sign_envelope({"x": 1}, self.KEY)
        signed[sh.HMAC_HEADER_FIELD] = "not-a-valid-header"
        with pytest.raises(sh.HmacVerifyError, match="malformed"):
            sh.verify_envelope(signed, self.KEY)

    def test_queue_url_uses_tls(self):
        assert sh.queue_url_uses_tls("rediss://host:6379/0") is True
        assert sh.queue_url_uses_tls("amqps://host/") is True
        assert sh.queue_url_uses_tls("https://host/") is True
        assert sh.queue_url_uses_tls("redis://host:6379/0") is False
        assert sh.queue_url_uses_tls("") is False

    def test_assert_production_queue_tls_blocks_plaintext_in_prod(self):
        with pytest.raises(RuntimeError, match="plaintext"):
            sh.assert_production_queue_tls("redis://x", env="production")

    def test_assert_production_queue_tls_warns_only_in_dev(self):
        # no raise
        sh.assert_production_queue_tls("redis://x", env="development")

    def test_key_from_env(self, monkeypatch):
        monkeypatch.setenv(sh.HMAC_ENV, "thekey")
        monkeypatch.setenv(sh.HMAC_KEY_ID_ENV, "kABC")
        k = sh.QueueHmacKey.from_env()
        assert k is not None
        assert k.key_id == "kABC"
        assert k.secret == b"thekey"

    def test_key_from_env_missing_returns_none(self, monkeypatch):
        monkeypatch.delenv(sh.HMAC_ENV, raising=False)
        assert sh.QueueHmacKey.from_env() is None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  2. Redis ACL role rendering
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestRedisAclRoles:
    def test_default_three_roles_present(self):
        roles = sh.default_redis_acl_roles()
        names = {r.username for r in roles}
        assert names == {
            "omnisight-orchestrator",
            "omnisight-worker",
            "omnisight-observer",
        }

    def test_observer_is_readonly(self):
        roles = {r.username: r for r in sh.default_redis_acl_roles()}
        obs = roles["omnisight-observer"]
        # No write verbs — @write must be DENIED.
        assert "-@write" in obs.commands
        assert "+@read" in obs.commands

    def test_worker_cannot_flush_or_admin(self):
        roles = {r.username: r for r in sh.default_redis_acl_roles()}
        wk = roles["omnisight-worker"]
        for deny in ("-flushdb", "-flushall", "-debug", "-shutdown",
                     "-config", "-cluster", "-acl"):
            assert deny in wk.commands, f"worker missing deny {deny}"

    def test_worker_cannot_manage_consumer_groups(self):
        roles = {r.username: r for r in sh.default_redis_acl_roles()}
        wk = roles["omnisight-worker"]
        assert "-xgroup" in wk.commands
        assert "-xtrim" in wk.commands

    def test_render_setuser_command(self):
        role = sh.default_redis_acl_roles()[0]
        cmd = role.to_setuser_command("deadbeef")
        assert cmd.startswith("ACL SETUSER omnisight-orchestrator")
        assert " on " in cmd
        assert "#deadbeef" in cmd
        assert "~omnisight:queue:*" in cmd

    def test_render_acl_file_has_warning_when_no_passwords(self):
        out = sh.render_acl_file()
        assert "WARNING" in out
        assert "user omnisight-orchestrator" in out

    def test_render_acl_file_uses_password_hashes(self):
        out = sh.render_acl_file(password_hashes={"omnisight-worker": "aaa"})
        assert "user omnisight-worker" in out
        assert "#aaa" in out


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  3. Worker attestation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _make_identity(worker_id="w1", tenant="t-acme", fp="fp-abc",
                   psk=b"psk-w1") -> sh.WorkerIdentity:
    return sh.WorkerIdentity(
        worker_id=worker_id,
        tenant_id=tenant,
        capabilities=("firmware",),
        tls_cert_fingerprint=fp,
        pre_shared_key=psk,
    )


class TestWorkerAttestation:
    def test_happy_path_verifies(self):
        ident = _make_identity()
        verifier = sh.AttestationVerifier(
            known_workers={"w1": ident}, allowed_tenants=("t-acme",),
        )
        token = sh.issue_attestation(ident)
        got = verifier.verify(token)
        assert got.worker_id == "w1"

    def test_replay_rejected(self):
        ident = _make_identity()
        verifier = sh.AttestationVerifier(known_workers={"w1": ident})
        token = sh.issue_attestation(ident)
        verifier.verify(token)
        with pytest.raises(sh.AttestationError, match="replay"):
            verifier.verify(token)

    def test_unknown_worker_rejected(self):
        verifier = sh.AttestationVerifier(known_workers={})
        ident = _make_identity()
        token = sh.issue_attestation(ident)
        with pytest.raises(sh.AttestationError, match="unknown worker"):
            verifier.verify(token)

    def test_tenant_allowlist_enforced(self):
        ident = _make_identity(tenant="t-acme")
        verifier = sh.AttestationVerifier(
            known_workers={"w1": ident}, allowed_tenants=("t-other",),
        )
        token = sh.issue_attestation(ident)
        with pytest.raises(sh.AttestationError, match="allowlist"):
            verifier.verify(token)

    def test_tls_fingerprint_mismatch_rejected(self):
        stored = _make_identity(fp="fp-original")
        presented = dataclasses.replace(stored, tls_cert_fingerprint="fp-other")
        verifier = sh.AttestationVerifier(known_workers={"w1": stored})
        token = sh.issue_attestation(presented)
        with pytest.raises(sh.AttestationError, match="TLS"):
            verifier.verify(token)

    def test_capability_tamper_rejected(self):
        stored = _make_identity()
        verifier = sh.AttestationVerifier(known_workers={"w1": stored})
        token = sh.issue_attestation(stored)
        # Tamper capability list post-sign.
        token["claim"]["capabilities"] = ["everything"]
        with pytest.raises(sh.AttestationError, match="capability|signature"):
            verifier.verify(token)

    def test_psk_mismatch_rejected(self):
        stored = _make_identity(psk=b"real-psk")
        verifier = sh.AttestationVerifier(known_workers={"w1": stored})
        # Attacker only knows the identity but signs with wrong PSK.
        attacker = dataclasses.replace(stored, pre_shared_key=b"guessed-psk")
        token = sh.issue_attestation(attacker)
        with pytest.raises(sh.AttestationError, match="signature"):
            verifier.verify(token)

    def test_expired_attestation_rejected(self):
        ident = _make_identity()
        verifier = sh.AttestationVerifier(
            known_workers={"w1": ident}, ttl_s=60,
        )
        token = sh.issue_attestation(ident, issued_at=time.time() - 3600)
        with pytest.raises(sh.AttestationError, match="expired"):
            verifier.verify(token)

    def test_version_check(self):
        ident = _make_identity()
        verifier = sh.AttestationVerifier(known_workers={"w1": ident})
        token = sh.issue_attestation(ident)
        token["claim"]["v"] = "v99"
        with pytest.raises(sh.AttestationError, match="version"):
            verifier.verify(token)

    def test_replay_cache_gc_after_ttl(self):
        ident = _make_identity()
        verifier = sh.AttestationVerifier(
            known_workers={"w1": ident}, ttl_s=1,
        )
        token = sh.issue_attestation(ident)
        verifier.verify(token)
        # Simulate a later time beyond TTL — GC drops the nonce so a NEW
        # token (different nonce) with a fresh iat verifies.  (Same
        # token can't re-verify because iat would be expired.)
        later = time.time() + 5
        fresh = sh.issue_attestation(ident, issued_at=later)
        got = verifier.verify(fresh, now=later)
        assert got.worker_id == "w1"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  4. Merger vote hash-chain
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestMergerVoteChain:
    def test_linear_chain_verifies(self):
        chain = sh.MergerVoteAuditChain(persist=False)
        chain.append(
            change_id="I1", patchset_revision="abc", vote=2,
            confidence=0.95, rationale="r1", reason_code="plus_two_voted",
        )
        chain.append(
            change_id="I1", patchset_revision="def", vote=0,
            confidence=0.3, rationale="r2", reason_code="abstained_low_confidence",
        )
        ok, bad = chain.verify()
        assert ok is True and bad is None

    def test_tamper_detected_at_first_bad_row(self):
        chain = sh.MergerVoteAuditChain(persist=False)
        chain.append(
            change_id="I1", patchset_revision="abc", vote=2,
            confidence=0.95, rationale="r1", reason_code="plus_two_voted",
        )
        chain.append(
            change_id="I1", patchset_revision="def", vote=2,
            confidence=0.95, rationale="r2", reason_code="plus_two_voted",
        )
        chain.append(
            change_id="I1", patchset_revision="ghi", vote=0,
            confidence=0.8, rationale="r3", reason_code="abstained_low_confidence",
        )
        # Flip vote in row 1 without recomputing hash.
        chain.entries[1]["vote"] = -2
        ok, bad = chain.verify()
        assert ok is False
        assert bad == 1

    def test_head_changes_per_append(self):
        chain = sh.MergerVoteAuditChain(persist=False)
        assert chain.head() == ""
        chain.append(
            change_id="I1", patchset_revision="abc", vote=2,
            confidence=0.9, rationale="r", reason_code="plus_two_voted",
        )
        h1 = chain.head()
        chain.append(
            change_id="I2", patchset_revision="def", vote=0,
            confidence=0.4, rationale="r", reason_code="abstained_low_confidence",
        )
        h2 = chain.head()
        assert h1 and h2 and h1 != h2

    def test_reject_out_of_range_vote(self):
        chain = sh.MergerVoteAuditChain(persist=False)
        with pytest.raises(ValueError, match="vote"):
            chain.append(
                change_id="I1", patchset_revision="abc", vote=7,
                confidence=0.5, rationale="r", reason_code="x",
            )

    def test_reject_missing_change_id(self):
        chain = sh.MergerVoteAuditChain(persist=False)
        with pytest.raises(ValueError, match="change_id"):
            chain.append(
                change_id="", patchset_revision="abc", vote=2,
                confidence=0.5, rationale="r", reason_code="x",
            )

    def test_for_change_filter(self):
        chain = sh.MergerVoteAuditChain(persist=False)
        chain.append(change_id="I1", patchset_revision="a", vote=2,
                     confidence=0.9, rationale="r", reason_code="x")
        chain.append(change_id="I2", patchset_revision="b", vote=0,
                     confidence=0.5, rationale="r", reason_code="y")
        chain.append(change_id="I1", patchset_revision="c", vote=0,
                     confidence=0.5, rationale="r", reason_code="z")
        assert len(chain.for_change("I1")) == 2
        assert len(chain.for_change("I2")) == 1
        assert chain.for_change("I3") == []

    def test_global_chain_singleton(self):
        sh.reset_global_merger_chain_for_tests()
        c1 = sh.get_global_merger_chain()
        c2 = sh.get_global_merger_chain()
        assert c1 is c2
        sh.reset_global_merger_chain_for_tests()
        c3 = sh.get_global_merger_chain()
        assert c3 is not c1


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  5. Gerrit permission verifier
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestGerritMergerLeastPrivilege:
    def test_real_project_config_example_passes(self):
        findings = sh.verify_merger_least_privilege(
            ".gerrit/project.config.example",
        )
        assert findings == [], (
            "current project.config.example grants forbidden permissions "
            "to ai-reviewer-bots: " + ", ".join(
                f.forbidden_token for f in findings
            )
        )

    def test_missing_file_returns_empty(self, tmp_path):
        findings = sh.verify_merger_least_privilege(tmp_path / "absent.config")
        assert findings == []

    def test_submit_grant_flagged(self, tmp_path):
        cfg = tmp_path / "project.config"
        cfg.write_text(
            '[access "refs/heads/*"]\n'
            '    submit = group ai-reviewer-bots\n'
        )
        findings = sh.verify_merger_least_privilege(cfg)
        assert len(findings) == 1
        assert findings[0].forbidden_token == "submit"

    def test_force_push_grant_flagged(self, tmp_path):
        cfg = tmp_path / "project.config"
        cfg.write_text(
            '[access "refs/*"]\n'
            '    push force = group ai-reviewer-bots\n'
        )
        findings = sh.verify_merger_least_privilege(cfg)
        assert any("push force" in f.forbidden_token for f in findings)

    def test_delete_grant_flagged(self, tmp_path):
        cfg = tmp_path / "project.config"
        cfg.write_text(
            '[access "refs/*"]\n'
            '    delete = group ai-reviewer-bots\n'
        )
        findings = sh.verify_merger_least_privilege(cfg)
        assert any(f.forbidden_token == "delete" for f in findings)

    def test_deny_lines_not_flagged(self, tmp_path):
        cfg = tmp_path / "project.config"
        cfg.write_text(
            '[access "refs/heads/*"]\n'
            '    deny submit = group ai-reviewer-bots\n'
            '    deny delete = group ai-reviewer-bots\n'
        )
        findings = sh.verify_merger_least_privilege(cfg)
        assert findings == []

    def test_allowed_label_prefix_not_flagged(self, tmp_path):
        cfg = tmp_path / "project.config"
        cfg.write_text(
            '[access "refs/heads/*"]\n'
            '    label-Code-Review = -2..+2 group ai-reviewer-bots\n'
        )
        findings = sh.verify_merger_least_privilege(cfg)
        assert findings == []

    def test_non_access_section_ignored(self, tmp_path):
        cfg = tmp_path / "project.config"
        cfg.write_text(
            '[project]\n'
            '    description = submit = group ai-reviewer-bots\n'
        )
        findings = sh.verify_merger_least_privilege(cfg)
        assert findings == []


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  CLI smoke
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestCli:
    def test_render_acl_outputs_file(self, capsys):
        rc = sh._cli_main(["render-acl"])
        assert rc == 0
        captured = capsys.readouterr()
        assert "user omnisight-orchestrator" in captured.out

    def test_verify_gerrit_config_ok(self, capsys):
        rc = sh._cli_main([
            "verify-gerrit-config",
            ".gerrit/project.config.example",
        ])
        assert rc == 0
        captured = capsys.readouterr()
        assert "OK" in captured.out

    def test_verify_gerrit_config_violation(self, tmp_path, capsys):
        cfg = tmp_path / "bad.config"
        cfg.write_text(
            '[access "refs/heads/*"]\n'
            '    submit = group ai-reviewer-bots\n'
        )
        rc = sh._cli_main(["verify-gerrit-config", str(cfg)])
        assert rc == 1
        captured = capsys.readouterr()
        assert "VIOLATIONS" in captured.out

    def test_help(self, capsys):
        rc = sh._cli_main(["help"])
        assert rc == 0
        captured = capsys.readouterr()
        assert "usage" in captured.out.lower()

    def test_unknown_command(self, capsys):
        rc = sh._cli_main(["not-a-command"])
        assert rc == 2
