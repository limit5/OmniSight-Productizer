"""FS.4.5 -- Shared email delivery HTTP helper tests."""

from __future__ import annotations

import httpx
import pytest

from backend.email_delivery.base import (
    EmailDeliveryConflictError,
    EmailDeliveryError,
    EmailDeliveryRateLimitError,
    InvalidEmailDeliveryTokenError,
    MissingEmailDeliveryScopeError,
)
from backend.email_delivery.http import raise_for_email_response


class TestEmailDeliveryHttpErrors:

    @pytest.mark.parametrize("status", [200, 202, 204, 302])
    def test_success_statuses_return(self, status):
        raise_for_email_response(httpx.Response(status), "resend")

    @pytest.mark.parametrize(
        "status,exc_type",
        [
            (401, InvalidEmailDeliveryTokenError),
            (403, MissingEmailDeliveryScopeError),
            (409, EmailDeliveryConflictError),
            (422, EmailDeliveryConflictError),
        ],
    )
    def test_status_maps_to_email_delivery_exception(self, status, exc_type):
        resp = httpx.Response(status, json={"message": "provider says no"})

        with pytest.raises(exc_type) as excinfo:
            raise_for_email_response(resp, "postmark")

        assert excinfo.value.status == status
        assert excinfo.value.provider == "postmark"
        assert str(excinfo.value) == "provider says no"

    def test_rate_limit_preserves_retry_after(self):
        resp = httpx.Response(
            429,
            json={"error": "slow down"},
            headers={"Retry-After": "17"},
        )

        with pytest.raises(EmailDeliveryRateLimitError) as excinfo:
            raise_for_email_response(resp, "resend")

        assert excinfo.value.retry_after == 17
        assert excinfo.value.status == 429
        assert excinfo.value.provider == "resend"

    def test_default_error_uses_provider_body_message(self):
        resp = httpx.Response(500, json={"errors": [{"message": "boom"}]})

        with pytest.raises(EmailDeliveryError) as excinfo:
            raise_for_email_response(resp, "aws-ses")

        assert excinfo.value.status == 500
        assert excinfo.value.provider == "aws-ses"
        assert str(excinfo.value) == "boom"
