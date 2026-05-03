"""FS.9.1 -- Todo SaaS auth + DB + email end-to-end scenario test.

This capstone mirrors the FS.1.7 and FS.2.6 pattern: use provider-mocked
adapters, render the generated app bundle, and assert that the handoff
artifacts line up across the already-landed FS rows.

Module-global state audit: this test writes no module-level mutable state;
all generated files live under ``tmp_path``, provider calls are scoped to
``respx.mock``, and subprocess execution is replaced by a per-test recorder.

Read-after-write timing audit: no parallel writes are introduced; the scenario
serializes scaffold render, provider setup, migration smoke, template render,
and email delivery in one async test.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import httpx
import respx

from backend.auth_provisioning import (
    SelfHostedAuthScaffoldOptions,
    render_self_hosted_auth_scaffold,
)
from backend.auth_provisioning.auth0 import Auth0AuthProvisionAdapter
from backend.db_provisioning import run_tenant_migrations
from backend.db_provisioning.supabase import (
    SUPABASE_API_BASE,
    SupabaseDBProvisionAdapter,
)
from backend.email_delivery import (
    EmailAddress,
    EmailTemplateRenderOptions,
    render_email_template,
)
from backend.email_delivery.resend import RESEND_API_BASE, ResendEmailDeliveryAdapter
from backend.nextjs_scaffolder import ScaffoldOptions, pilot_report, render_project


APP_BASE_URL = "https://todo.example.com"
APP_NAME = "todo-saas"
SUPABASE = SUPABASE_API_BASE
RESEND = RESEND_API_BASE


def _ok(result=None, status=200):
    return httpx.Response(status, json=result if result is not None else {})


class _RunRecorder:
    def __init__(self) -> None:
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        return SimpleNamespace(returncode=0, stdout="todo db smoke ok", stderr="")


def _todo_bundle_options() -> ScaffoldOptions:
    return ScaffoldOptions(
        project_name=APP_NAME,
        auth="nextauth",
        trpc=True,
        prisma=True,
        resend=True,
        target="both",
        compliance=True,
        backend_url="http://localhost:8000",
        example_app="todo",
    )


def _assert_todo_bundle(project_dir: Path) -> None:
    package_json = json.loads((project_dir / "package.json").read_text())
    for dep in ("next-auth", "@trpc/server", "@prisma/client", "resend"):
        assert dep in package_json["dependencies"]

    schema = (project_dir / "prisma" / "schema.prisma").read_text()
    auth_config = (project_dir / "auth" / "nextauth.config.ts").read_text()
    contact_route = (project_dir / "app" / "api" / "contact" / "route.ts").read_text()
    todo_page = (project_dir / "app" / "todos" / "page.tsx").read_text()
    todo_app = (project_dir / "components" / "TodoApp.tsx").read_text()

    assert 'url      = env("DATABASE_URL")' in schema
    assert "model User" in schema
    assert "model Message" in schema
    assert "/api/v1/auth/login" in auth_config
    assert "sendContactEmail" in contact_route
    assert "db.message.create" in contact_route
    assert 'role="main"' in todo_page
    assert "useState<Todo[]>" in todo_app


async def _setup_auth0_login_config():
    api_base = "https://tenant.us.auth0.com/api/v2"
    callback = f"{APP_BASE_URL}/api/auth/callback/auth0"
    respx.get(f"{api_base}/clients").mock(return_value=_ok([]))
    respx.post(f"{api_base}/clients").mock(
        return_value=_ok(
            {
                "client_id": "client_todo_123",
                "client_secret": "secret_todo_123",
                "name": APP_NAME,
                "app_type": "regular_web",
                "callbacks": [callback],
                "web_origins": [APP_BASE_URL],
            },
            status=201,
        )
    )

    adapter = Auth0AuthProvisionAdapter(
        token="mgmt_ABCDEF0123456789",
        application_name=APP_NAME,
        tenant_domain="tenant.us.auth0.com",
    )
    result = await adapter.setup_application(
        redirect_uris=(callback,),
        allowed_origins=(APP_BASE_URL,),
    )
    scaffold = render_self_hosted_auth_scaffold(
        SelfHostedAuthScaffoldOptions(
            framework="nextauth",
            provider_setup=result,
            app_base_url=APP_BASE_URL,
            oauth_client_import="../oauth-client",
        )
    )
    text = "\n".join(f.content for f in scaffold.files)
    assert result.provider == "auth0"
    assert result.client_id == "client_todo_123"
    assert result.redirect_uris == (callback,)
    assert "DEFAULT_STATE_TTL_SECONDS" in text
    assert 'checks: ["pkce", "state"]' in text
    assert "secret_todo_123" not in text
    return result


async def _provision_supabase_and_smoke(monkeypatch, tmp_path: Path) -> str:
    rec = _RunRecorder()
    monkeypatch.setattr(subprocess, "run", rec)
    respx.get(f"{SUPABASE}/projects").mock(return_value=_ok([]))
    respx.post(f"{SUPABASE}/projects").mock(
        return_value=_ok(
            {
                "id": "prj_todo_123",
                "ref": "abcdefghijklmnopqrst",
                "organization_id": "org_todo",
                "name": APP_NAME,
                "region": "us-east-1",
                "status": "ACTIVE",
                "database": {"host": "db.abcdefghijklmnopqrst.supabase.co"},
            },
            status=201,
        )
    )

    provision = await SupabaseDBProvisionAdapter(
        token="sbp_ABCDEF0123456789",
        database_name=APP_NAME,
        organization_id="org_todo",
        provider_tier="team",
    ).provision_database(db_pass="p=word")

    assert provision.provider == "supabase"
    assert provision.created is True
    assert provision.connection_url is not None
    assert provision.encryption_at_rest is not None
    assert provision.backup_schedule is not None
    assert provision.pep_hold is not None

    migration = run_tenant_migrations(
        "prisma",
        connection_url=provision.connection_url,
        cwd=tmp_path,
        command=["pnpm", "prisma", "migrate", "deploy"],
        extra_env={"OMNISIGHT_SCENARIO": "fs-9-1-todo-saas"},
    )
    assert migration.ok is True
    assert migration.stdout == "todo db smoke ok"
    argv, kwargs = rec.calls[-1]
    assert argv == ["pnpm", "prisma", "migrate", "deploy"]
    assert kwargs["cwd"] == tmp_path
    assert kwargs["env"]["DATABASE_URL"] == provision.connection_url
    assert kwargs["env"]["OMNISIGHT_SCENARIO"] == "fs-9-1-todo-saas"
    return provision.connection_url


async def _send_welcome_email() -> None:
    route = respx.post(f"{RESEND}/emails").mock(
        return_value=httpx.Response(200, json={"id": "em_todo_welcome"})
    )
    message = render_email_template(
        EmailTemplateRenderOptions(
            template_id="welcome",
            sender=EmailAddress("noreply@todo.example.com", "Todo SaaS"),
            to=(EmailAddress("owner@todo.example.com", "Owner"),),
            context={
                "user_name": "Owner",
                "product_name": "Todo SaaS",
                "app_url": APP_BASE_URL,
                "support_email": "support@todo.example.com",
            },
            tags={"scenario": "fs-9-1"},
        )
    )

    result = await ResendEmailDeliveryAdapter(
        token="re_ABCDEF0123456789"
    ).send_email(message)

    assert result.provider == "resend"
    assert result.message_id == "em_todo_welcome"
    assert result.accepted == ["owner@todo.example.com"]
    body = httpx.Response(200, content=route.calls.last.request.read()).json()
    assert body["from"] == "Todo SaaS <noreply@todo.example.com>"
    assert body["to"] == ["Owner <owner@todo.example.com>"]
    assert body["tags"] == [
        {"name": "scenario", "value": "fs-9-1"},
        {"name": "template", "value": "welcome"},
    ]


@respx.mock
async def test_todo_saas_auth_db_email_complete_e2e(monkeypatch, tmp_path):
    project_dir = tmp_path / APP_NAME
    opts = _todo_bundle_options()

    outcome = render_project(project_dir, opts)
    assert outcome.warnings == []
    _assert_todo_bundle(project_dir)

    auth_result = await _setup_auth0_login_config()
    connection_url = await _provision_supabase_and_smoke(monkeypatch, project_dir)
    await _send_welcome_email()

    report = pilot_report(project_dir, opts)
    assert report["options"]["example_app"] == "todo"
    assert report["w5_compliance"]["failed_count"] == 0

    assert auth_result.issuer_url == "https://tenant.us.auth0.com/"
    assert connection_url.startswith("postgresql://postgres.")
