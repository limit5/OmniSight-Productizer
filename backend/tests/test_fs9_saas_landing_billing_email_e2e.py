"""FS.9.3 -- SaaS landing + billing + email end-to-end scenario test.

This capstone mirrors FS.9.1/FS.9.2: use provider-mocked adapters, render
the generated app bundle, and assert that the handoff artifacts line up
across the already-landed FS rows.

Module-global state audit: this test writes no module-level mutable state;
all generated files live under ``tmp_path`` and every provider call is scoped
to ``respx.mock`` routes for the current test.

Read-after-write timing audit: no parallel writes are introduced; the scenario
serializes scaffold render, Stripe checkout, Stripe portal, template render,
and email delivery in one async test.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import respx

from backend.email_delivery import (
    EmailAddress,
    EmailTemplateRenderOptions,
    render_email_template,
)
from backend.email_delivery.resend import RESEND_API_BASE, ResendEmailDeliveryAdapter
from backend.nextjs_scaffolder import ScaffoldOptions, pilot_report, render_project
from backend.stripe_billing import (
    STRIPE_API_BASE_URL,
    StripeBillingConfig,
    create_billing_portal_session,
    create_checkout_session,
    session_response,
)


APP_BASE_URL = "https://launch.example.com"
APP_NAME = "launch-saas"
RESEND = RESEND_API_BASE
STRIPE = STRIPE_API_BASE_URL


def _landing_bundle_options() -> ScaffoldOptions:
    return ScaffoldOptions(
        project_name=APP_NAME,
        auth="none",
        trpc=False,
        prisma=False,
        resend=True,
        target="both",
        compliance=True,
        backend_url="http://localhost:8000",
    )


def _assert_landing_bundle(project_dir: Path) -> None:
    package_json = json.loads((project_dir / "package.json").read_text())
    for dep in ("next", "react", "react-dom", "resend"):
        assert dep in package_json["dependencies"]

    home_page = (project_dir / "app" / "page.tsx").read_text()
    contact_route = (project_dir / "app" / "api" / "contact" / "route.ts").read_text()
    email_server = (project_dir / "server" / "email.ts").read_text()
    vercel = (project_dir / "vercel.json").read_text()
    wrangler = (project_dir / "wrangler.toml").read_text()

    assert "<h1>" in home_page
    assert "loadStats" in home_page
    assert 'role="main"' in home_page
    assert "sendContactEmail" in contact_route
    assert "CONTACT_TO_EMAIL" in email_server
    assert "RESEND_FROM_EMAIL" in email_server
    assert "framework" in vercel
    assert APP_NAME in wrangler


def _billing_config() -> StripeBillingConfig:
    return StripeBillingConfig(
        secret_key="sk_test_fs93",
        checkout_price_id="price_launch_monthly",
        checkout_success_url=f"{APP_BASE_URL}/billing/success",
        checkout_cancel_url=f"{APP_BASE_URL}/pricing",
        portal_return_url=f"{APP_BASE_URL}/billing",
    )


async def _create_billing_sessions() -> tuple[dict[str, str], dict[str, str]]:
    checkout_route = respx.post(f"{STRIPE}/checkout/sessions").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "cs_launch_123",
                "object": "checkout.session",
                "url": "https://checkout.stripe.test/launch",
                "client_secret": "hidden",
            },
        ),
    )
    portal_route = respx.post(f"{STRIPE}/billing_portal/sessions").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "bps_launch_123",
                "object": "billing_portal.session",
                "url": "https://billing.stripe.test/launch",
            },
        ),
    )

    checkout = await create_checkout_session(
        _billing_config(),
        tenant_id="tenant-launch",
        user_id="user-owner",
        user_email="owner@launch.example.com",
        price_id="price_launch_annual",
        success_url=f"{APP_BASE_URL}/billing/success?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{APP_BASE_URL}/pricing?billing=cancelled",
    )
    portal = await create_billing_portal_session(
        _billing_config(),
        customer_id="cus_launch_123",
        return_url=f"{APP_BASE_URL}/account/billing",
    )

    checkout_body = parse_qs(checkout_route.calls.last.request.content.decode())
    assert checkout_body == {
        "mode": ["subscription"],
        "line_items[0][price]": ["price_launch_annual"],
        "line_items[0][quantity]": ["1"],
        "success_url": [
            f"{APP_BASE_URL}/billing/success?session_id={{CHECKOUT_SESSION_ID}}"
        ],
        "cancel_url": [f"{APP_BASE_URL}/pricing?billing=cancelled"],
        "client_reference_id": ["tenant-launch"],
        "metadata[tenant_id]": ["tenant-launch"],
        "metadata[user_id]": ["user-owner"],
        "subscription_data[metadata][tenant_id]": ["tenant-launch"],
        "subscription_data[metadata][user_id]": ["user-owner"],
        "customer_email": ["owner@launch.example.com"],
    }

    portal_body = parse_qs(portal_route.calls.last.request.content.decode())
    assert portal_body == {
        "customer": ["cus_launch_123"],
        "return_url": [f"{APP_BASE_URL}/account/billing"],
    }

    return session_response(checkout), session_response(portal)


async def _send_launch_email() -> None:
    route = respx.post(f"{RESEND}/emails").mock(
        return_value=httpx.Response(200, json={"id": "em_launch_welcome"})
    )
    message = render_email_template(
        EmailTemplateRenderOptions(
            template_id="welcome",
            sender=EmailAddress("noreply@launch.example.com", "Launch SaaS"),
            to=(EmailAddress("owner@launch.example.com", "Owner"),),
            context={
                "user_name": "Owner",
                "product_name": "Launch SaaS",
                "app_url": APP_BASE_URL,
                "support_email": "support@launch.example.com",
            },
            tags={"scenario": "fs-9-3"},
        )
    )

    result = await ResendEmailDeliveryAdapter(
        token="re_ABCDEF0123456789"
    ).send_email(message)

    assert result.provider == "resend"
    assert result.message_id == "em_launch_welcome"
    assert result.accepted == ["owner@launch.example.com"]
    body = httpx.Response(200, content=route.calls.last.request.read()).json()
    assert body["from"] == "Launch SaaS <noreply@launch.example.com>"
    assert body["to"] == ["Owner <owner@launch.example.com>"]
    assert body["tags"] == [
        {"name": "scenario", "value": "fs-9-3"},
        {"name": "template", "value": "welcome"},
    ]


@respx.mock
async def test_saas_landing_billing_email_complete_e2e(tmp_path):
    project_dir = tmp_path / APP_NAME
    opts = _landing_bundle_options()

    outcome = render_project(project_dir, opts)
    assert outcome.warnings == []
    _assert_landing_bundle(project_dir)

    checkout, portal = await _create_billing_sessions()
    await _send_launch_email()

    report = pilot_report(project_dir, opts)
    assert report["options"]["auth"] == "none"
    assert report["options"]["resend"] is True
    assert report["w4_deploy"]["vercel"]["artifact_valid"] is True
    assert report["w4_deploy"]["cloudflare"]["artifact_valid"] is True
    assert report["w5_compliance"]["failed_count"] == 0

    assert checkout == {
        "id": "cs_launch_123",
        "url": "https://checkout.stripe.test/launch",
        "object": "checkout.session",
    }
    assert portal == {
        "id": "bps_launch_123",
        "url": "https://billing.stripe.test/launch",
        "object": "billing_portal.session",
    }
