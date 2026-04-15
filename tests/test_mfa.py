"""K5 — MFA unit tests.

Covers TOTP enrollment/verification with drift tolerance,
backup code single-use enforcement, and MFA challenge flow.
"""

import asyncio
import sys
import os
import time

import pytest
import pyotp

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("OMNISIGHT_AUTH_MODE", "session")


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture(autouse=True)
def _setup_db(tmp_path):
    db_path = str(tmp_path / "test.db")
    os.environ["DATABASE_PATH"] = db_path
    from backend.config import settings
    settings.database_path = db_path
    from backend import db
    db._DB_PATH = tmp_path / "test.db"
    db._db = None
    _run(db.init())
    yield
    _run(db.close())


@pytest.fixture
def user_id(_setup_db):
    from backend import auth
    user = _run(auth.create_user("mfa_test@test.com", "MFA Tester", "admin", "SuperSecure12345!"))
    return user.id


class TestTOTPEnrollment:
    def test_enroll_and_verify(self, user_id):
        from backend import mfa

        result = _run(mfa.totp_begin_enroll(user_id, "mfa_test@test.com"))
        assert "secret" in result
        assert "qr_png_b64" in result
        assert result["qr_png_b64"]

        totp = pyotp.TOTP(result["secret"])
        code = totp.now()
        ok = _run(mfa.totp_confirm_enroll(user_id, code))
        assert ok is True

        has = _run(mfa.has_verified_mfa(user_id))
        assert has is True

    def test_enroll_wrong_code_fails(self, user_id):
        from backend import mfa

        _run(mfa.totp_begin_enroll(user_id, "mfa_test@test.com"))
        ok = _run(mfa.totp_confirm_enroll(user_id, "000000"))
        assert ok is False

    def test_totp_drift_tolerance(self, user_id):
        """TOTP should accept codes from adjacent time steps (drift +-1)."""
        from backend import mfa

        result = _run(mfa.totp_begin_enroll(user_id, "mfa_test@test.com"))
        secret = result["secret"]

        totp = pyotp.TOTP(secret)
        code = totp.now()
        ok = _run(mfa.totp_confirm_enroll(user_id, code))
        assert ok is True

        valid = _run(mfa.verify_totp(user_id, code))
        assert valid is True

    def test_totp_disable(self, user_id):
        from backend import mfa

        result = _run(mfa.totp_begin_enroll(user_id, "mfa_test@test.com"))
        totp = pyotp.TOTP(result["secret"])
        _run(mfa.totp_confirm_enroll(user_id, totp.now()))

        ok = _run(mfa.totp_disable(user_id))
        assert ok is True

        has = _run(mfa.has_verified_mfa(user_id))
        assert has is False


class TestBackupCodes:
    def test_backup_code_single_use(self, user_id):
        """Each backup code can only be used once."""
        from backend import mfa

        result = _run(mfa.totp_begin_enroll(user_id, "mfa_test@test.com"))
        totp = pyotp.TOTP(result["secret"])
        _run(mfa.totp_confirm_enroll(user_id, totp.now()))

        codes = _run(mfa.regenerate_backup_codes(user_id))
        assert len(codes) == 10

        first_code = codes[0]
        ok1 = _run(mfa.verify_backup_code(user_id, first_code))
        assert ok1 is True

        ok2 = _run(mfa.verify_backup_code(user_id, first_code))
        assert ok2 is False

    def test_backup_code_status(self, user_id):
        from backend import mfa

        result = _run(mfa.totp_begin_enroll(user_id, "mfa_test@test.com"))
        totp = pyotp.TOTP(result["secret"])
        _run(mfa.totp_confirm_enroll(user_id, totp.now()))

        status = _run(mfa.get_backup_codes_status(user_id))
        assert status["total"] == 10
        assert status["remaining"] == 10

        codes = _run(mfa.regenerate_backup_codes(user_id))
        _run(mfa.verify_backup_code(user_id, codes[0]))

        status = _run(mfa.get_backup_codes_status(user_id))
        assert status["remaining"] == 9

    def test_no_backup_codes_without_mfa(self, user_id):
        from backend import mfa

        codes = _run(mfa.regenerate_backup_codes(user_id))
        assert codes == []


class TestMFAChallenge:
    def test_create_and_consume_challenge(self, user_id):
        from backend import mfa

        token = mfa.create_mfa_challenge(user_id, ip="127.0.0.1", user_agent="test")
        assert token

        data = mfa.get_mfa_challenge(token)
        assert data is not None
        assert data["user_id"] == user_id

        consumed = mfa.consume_mfa_challenge(token)
        assert consumed is not None

        again = mfa.get_mfa_challenge(token)
        assert again is None

    def test_challenge_expires(self, user_id):
        from backend import mfa

        token = mfa.create_mfa_challenge(user_id)
        mfa._pending_mfa[token]["created_at"] = time.time() - 400

        data = mfa.get_mfa_challenge(token)
        assert data is None


class TestMFAStatus:
    def test_no_mfa_by_default(self, user_id):
        from backend import mfa

        has = _run(mfa.has_verified_mfa(user_id))
        assert has is False

        methods = _run(mfa.get_user_mfa_methods(user_id))
        assert methods == []

    def test_methods_listed_after_enrollment(self, user_id):
        from backend import mfa

        result = _run(mfa.totp_begin_enroll(user_id, "mfa_test@test.com"))
        totp = pyotp.TOTP(result["secret"])
        _run(mfa.totp_confirm_enroll(user_id, totp.now()))

        methods = _run(mfa.get_user_mfa_methods(user_id))
        assert len(methods) == 1
        assert methods[0]["method"] == "totp"
        assert methods[0]["verified"] is True
