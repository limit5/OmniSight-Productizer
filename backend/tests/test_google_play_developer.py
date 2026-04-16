"""P5 (#290) — Unit tests for backend.google_play_developer."""

from __future__ import annotations

import json

import pytest

from backend import google_play_developer as gpd


_VALID_PEM = (
    "-----BEGIN RSA PRIVATE KEY-----\n"
    "MIIEowIBAAKCAQEA+OfflinePlaceholderRSAKeyUsedForTestsOnly+\n"
    "-----END RSA PRIVATE KEY-----\n"
)


def _fake_signer(message: bytes, pem: str) -> bytes:
    return (b"rs256-" + message[:24] + b"-" + pem[-8:].encode("utf-8")).ljust(
        256, b"\x00",
    )[:256]


@pytest.fixture()
def creds():
    return gpd.GooglePlayCredentials(
        client_email="ci@omnisight.iam.gserviceaccount.com",
        private_key_pem=_VALID_PEM,
        package_name="com.acme.consumer",
        project_id="omnisight",
    )


@pytest.fixture()
def client(creds):
    clock = {"t": 1_700_000_000.0}

    def _clock():
        return clock["t"]

    def _fake_exchange(assertion, uri):
        return {"access_token": "fake-bearer", "expires_at": _clock() + 3600}

    ft = gpd.FakeTransport()
    c = gpd.GooglePlayClient(
        creds, transport=ft, signer=_fake_signer, clock=_clock,
        token_exchange=_fake_exchange,
    )
    return c, ft, clock


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Credentials
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestCredentials:
    def test_valid(self, creds):
        assert creds.package_name == "com.acme.consumer"

    def test_redacted_scrubs_pem(self, creds):
        v = creds.redacted()
        assert _VALID_PEM not in json.dumps(v)
        assert v["private_key_fingerprint"].startswith("sha256:")

    @pytest.mark.parametrize("bad_email", ["", "noat"])
    def test_rejects_bad_email(self, bad_email):
        with pytest.raises(gpd.PlayInvalidCredentialsError):
            gpd.GooglePlayCredentials(
                client_email=bad_email,
                private_key_pem=_VALID_PEM,
                package_name="com.acme.x",
            )

    def test_rejects_non_pem(self):
        with pytest.raises(gpd.PlayInvalidCredentialsError):
            gpd.GooglePlayCredentials(
                client_email="a@b.com",
                private_key_pem="not-pem",
                package_name="com.acme.x",
            )

    @pytest.mark.parametrize("bad_pkg", ["", "acme", "com..acme", "..acme.x"])
    def test_rejects_bad_package(self, bad_pkg):
        with pytest.raises(gpd.PlayInvalidCredentialsError):
            gpd.GooglePlayCredentials(
                client_email="a@b.com",
                private_key_pem=_VALID_PEM,
                package_name=bad_pkg,
            )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  JWT assertion
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestAssertionJWT:
    def test_shape(self, creds):
        bundle = gpd.issue_service_account_jwt(
            creds, now=1_700_000_000.0, signer=_fake_signer,
        )
        assert set(bundle.keys()) == {"assertion", "expires_at"}
        import base64
        head_b64, payload_b64, _ = bundle["assertion"].split(".")
        header = json.loads(base64.urlsafe_b64decode(head_b64 + "==="))
        payload = json.loads(base64.urlsafe_b64decode(payload_b64 + "==="))
        assert header == {"alg": "RS256", "typ": "JWT"}
        assert payload["iss"] == creds.client_email
        assert payload["scope"] == gpd.PLAY_SCOPE
        assert payload["aud"] == gpd.PLAY_TOKEN_URL
        assert payload["exp"] - payload["iat"] == 3600


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Edit session
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestEdit:
    def test_open_and_commit(self, client):
        c, ft, _ = client
        ft.push(gpd.TransportResponse(200, {"id": "edit-42"}))  # open
        ft.push(gpd.TransportResponse(200, {}))                  # commit
        with c.new_edit() as edit:
            assert edit.edit_id == "edit-42"
        assert edit._finalised
        # two calls — open + commit
        assert ft.calls[-1]["url"].endswith(":commit")

    def test_abort_on_exception(self, client):
        c, ft, _ = client
        ft.push(gpd.TransportResponse(200, {"id": "edit-99"}))  # open
        ft.push(gpd.TransportResponse(204, {}))                  # DELETE
        with pytest.raises(RuntimeError):
            with c.new_edit():
                raise RuntimeError("boom")
        assert ft.calls[-1]["method"] == "DELETE"

    def test_commit_before_open_raises(self, client):
        c, _, _ = client
        edit = gpd.GooglePlayEdit(c)
        with pytest.raises(gpd.PlayEditError):
            edit.commit()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Upload bundle
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestUploadBundle:
    def test_happy(self, client):
        c, ft, _ = client
        ft.push(gpd.TransportResponse(200, {"id": "e-1"}))  # open
        ft.push(gpd.TransportResponse(200, {
            "versionCode": 42,
            "sha256": "a" * 64,
            "sha1": "b" * 40,
        }))
        ft.push(gpd.TransportResponse(200, {}))  # commit

        with c.new_edit() as edit:
            b = c.upload_bundle(
                edit=edit,
                aab_sha256="a" * 64,
                aab_sha1="b" * 40,
                version_code=42,
            )
        assert b.version_code == 42
        assert b.package_name == "com.acme.consumer"

    def test_requires_open_edit(self, client):
        c, _, _ = client
        edit = gpd.GooglePlayEdit(c)  # never opened
        with pytest.raises(gpd.PlayEditError):
            c.upload_bundle(
                edit=edit, aab_sha256="a" * 64, aab_sha1="b" * 40,
                version_code=1,
            )

    def test_rejects_bad_hash(self, client):
        c, ft, _ = client
        ft.push(gpd.TransportResponse(200, {"id": "e-1"}))
        with c.new_edit() as edit:
            with pytest.raises(gpd.GooglePlayError):
                c.upload_bundle(
                    edit=edit, aab_sha256="x", aab_sha1="b" * 40,
                    version_code=1,
                )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Staged-rollout invariants
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class _Ctx:
    allow = True
    reason = "ok"
    audit_entry = {"curr_hash": "x"}


class TestStagedRollout:
    def test_internal_rejects_in_progress(self):
        with pytest.raises(gpd.PlayRolloutError):
            gpd._validate_rollout(
                track=gpd.Track.internal,
                status=gpd.TrackStatus.in_progress,
                user_fraction=0.1,
            )

    def test_completed_requires_full_fraction(self):
        with pytest.raises(gpd.PlayRolloutError):
            gpd._validate_rollout(
                track=gpd.Track.production,
                status=gpd.TrackStatus.completed,
                user_fraction=0.5,
            )

    def test_in_progress_requires_fraction_below_one(self):
        with pytest.raises(gpd.PlayRolloutError):
            gpd._validate_rollout(
                track=gpd.Track.production,
                status=gpd.TrackStatus.in_progress,
                user_fraction=1.0,
            )

    def test_fraction_must_be_positive_for_in_progress(self):
        with pytest.raises(gpd.PlayRolloutError):
            gpd._validate_rollout(
                track=gpd.Track.production,
                status=gpd.TrackStatus.in_progress,
                user_fraction=0.0,
            )

    def test_valid_staged_rollout(self):
        gpd._validate_rollout(
            track=gpd.Track.production,
            status=gpd.TrackStatus.in_progress,
            user_fraction=0.1,
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  update_track  (production requires dual-sign)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestUpdateTrack:
    def test_production_requires_dual_sign(self, client):
        c, ft, _ = client
        ft.push(gpd.TransportResponse(200, {"id": "e-1"}))
        with c.new_edit() as edit:
            with pytest.raises(gpd.PlayMissingDualSignError):
                c.update_track(
                    edit=edit,
                    track=gpd.Track.production,
                    version_codes=[1],
                    status=gpd.TrackStatus.completed,
                    user_fraction=1.0,
                )

    def test_internal_track_no_context_needed(self, client):
        c, ft, _ = client
        ft.push(gpd.TransportResponse(200, {"id": "e-1"}))
        ft.push(gpd.TransportResponse(200, {}))
        ft.push(gpd.TransportResponse(200, {}))  # commit
        with c.new_edit() as edit:
            update = c.update_track(
                edit=edit,
                track=gpd.Track.internal,
                version_codes=[1],
                status=gpd.TrackStatus.completed,
                user_fraction=1.0,
            )
        assert update.track is gpd.Track.internal

    def test_empty_version_codes_rejected(self, client):
        c, ft, _ = client
        ft.push(gpd.TransportResponse(200, {"id": "e-1"}))
        with c.new_edit() as edit:
            with pytest.raises(gpd.GooglePlayError):
                c.update_track(
                    edit=edit,
                    track=gpd.Track.internal,
                    version_codes=[],
                    status=gpd.TrackStatus.completed,
                    user_fraction=1.0,
                )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  submit_to_production end-to-end
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestSubmitToProduction:
    def test_staged_rollout_ok(self, client):
        c, ft, _ = client
        ft.push(gpd.TransportResponse(200, {"id": "e-1"}))  # open
        ft.push(gpd.TransportResponse(200, {}))              # patch
        ft.push(gpd.TransportResponse(200, {}))              # commit
        update = c.submit_to_production(
            version_code=42,
            dual_sign_context=_Ctx(),
            user_fraction=0.1,
            release_notes={"en-US": "First release"},
        )
        assert update.status is gpd.TrackStatus.in_progress
        assert update.user_fraction == 0.1

    def test_full_rollout_becomes_completed(self, client):
        c, ft, _ = client
        ft.push(gpd.TransportResponse(200, {"id": "e-2"}))
        ft.push(gpd.TransportResponse(200, {}))
        ft.push(gpd.TransportResponse(200, {}))
        update = c.submit_to_production(
            version_code=42,
            dual_sign_context=_Ctx(),
            user_fraction=1.0,
        )
        assert update.status is gpd.TrackStatus.completed
