---
audience: operator
---

# OAuth Providers Setup

> FX2.D9.7.14. Audience: operators creating OAuth apps for OmniSight
> self-login.

## Overview

OmniSight supports 11 "Sign in with X" providers through:

- `GET /api/v1/auth/oauth/{provider}/authorize`
- `GET /api/v1/auth/oauth/{provider}/callback`

The backend derives each callback URL from `OMNISIGHT_OAUTH_REDIRECT_BASE_URL`.
For production, set that base URL explicitly so every provider sees the same
public origin that users use in the browser.

```bash
OMNISIGHT_OAUTH_REDIRECT_BASE_URL=https://omnisight.example.com
```

The callback URI registered at each provider is:

```text
https://omnisight.example.com/api/v1/auth/oauth/<provider>/callback
```

For local development, replace the base with the public dev origin the provider
can reach, for example:

```text
http://localhost:8000/api/v1/auth/oauth/google/callback
https://<ngrok-host>.ngrok-free.app/api/v1/auth/oauth/google/callback
```

Use HTTPS for production callbacks. Some providers reject plain HTTP except for
localhost development.

## Shared Checklist

1. Pick the OmniSight public base URL.
2. Set `OMNISIGHT_OAUTH_REDIRECT_BASE_URL` to that base URL.
3. Create one OAuth app per provider in the vendor console.
4. Register exactly one callback URI per provider:
   `https://<base>/api/v1/auth/oauth/<provider>/callback`.
5. Request only the scopes listed below.
6. Copy the provider client ID and client secret into server-side env vars.
7. Set the frontend-safe configured flag for enabled providers:
   `NEXT_PUBLIC_OMNISIGHT_OAUTH_<PROVIDER>_CONFIGURED=true`.
8. Restart backend and rebuild/restart frontend.
9. Smoke from `/login` by clicking the provider button and confirming the
   callback creates a session.

Do not commit client secrets. Store them with the same production secret path
used for other OmniSight runtime environment variables.

## Environment Variables

| Provider | Server env vars | Frontend configured flag |
|---|---|---|
| Google | `OMNISIGHT_OAUTH_GOOGLE_CLIENT_ID`, `OMNISIGHT_OAUTH_GOOGLE_CLIENT_SECRET` | `NEXT_PUBLIC_OMNISIGHT_OAUTH_GOOGLE_CONFIGURED=true` |
| GitHub | `OMNISIGHT_OAUTH_GITHUB_CLIENT_ID`, `OMNISIGHT_OAUTH_GITHUB_CLIENT_SECRET` | `NEXT_PUBLIC_OMNISIGHT_OAUTH_GITHUB_CONFIGURED=true` |
| Microsoft | `OMNISIGHT_OAUTH_MICROSOFT_CLIENT_ID`, `OMNISIGHT_OAUTH_MICROSOFT_CLIENT_SECRET` | `NEXT_PUBLIC_OMNISIGHT_OAUTH_MICROSOFT_CONFIGURED=true` |
| Apple | `OMNISIGHT_OAUTH_APPLE_CLIENT_ID`, `OMNISIGHT_OAUTH_APPLE_CLIENT_SECRET` | `NEXT_PUBLIC_OMNISIGHT_OAUTH_APPLE_CONFIGURED=true` |
| Discord | `OMNISIGHT_OAUTH_DISCORD_CLIENT_ID`, `OMNISIGHT_OAUTH_DISCORD_CLIENT_SECRET` | `NEXT_PUBLIC_OMNISIGHT_OAUTH_DISCORD_CONFIGURED=true` |
| GitLab | `OMNISIGHT_OAUTH_GITLAB_CLIENT_ID`, `OMNISIGHT_OAUTH_GITLAB_CLIENT_SECRET` | `NEXT_PUBLIC_OMNISIGHT_OAUTH_GITLAB_CONFIGURED=true` |
| Bitbucket | `OMNISIGHT_OAUTH_BITBUCKET_CLIENT_ID`, `OMNISIGHT_OAUTH_BITBUCKET_CLIENT_SECRET` | `NEXT_PUBLIC_OMNISIGHT_OAUTH_BITBUCKET_CONFIGURED=true` |
| Slack | `OMNISIGHT_OAUTH_SLACK_CLIENT_ID`, `OMNISIGHT_OAUTH_SLACK_CLIENT_SECRET` | `NEXT_PUBLIC_OMNISIGHT_OAUTH_SLACK_CONFIGURED=true` |
| Notion | `OMNISIGHT_OAUTH_NOTION_CLIENT_ID`, `OMNISIGHT_OAUTH_NOTION_CLIENT_SECRET` | `NEXT_PUBLIC_OMNISIGHT_OAUTH_NOTION_CONFIGURED=true` |
| Salesforce | `OMNISIGHT_OAUTH_SALESFORCE_CLIENT_ID`, `OMNISIGHT_OAUTH_SALESFORCE_CLIENT_SECRET`; optional `OMNISIGHT_OAUTH_SALESFORCE_LOGIN_BASE_URL` | `NEXT_PUBLIC_OMNISIGHT_OAUTH_SALESFORCE_CONFIGURED=true` |
| HubSpot | `OMNISIGHT_OAUTH_HUBSPOT_CLIENT_ID`, `OMNISIGHT_OAUTH_HUBSPOT_CLIENT_SECRET` | `NEXT_PUBLIC_OMNISIGHT_OAUTH_HUBSPOT_CONFIGURED=true` |

The frontend can alternatively use
`NEXT_PUBLIC_OMNISIGHT_OAUTH_<PROVIDER>_CLIENT_ID` plus
`NEXT_PUBLIC_OMNISIGHT_OAUTH_<PROVIDER>_CLIENT_SECRET_CONFIGURED=true`, but the
single `*_CONFIGURED=true` flag is the lowest-leakage operator path.

## Provider Summary

| Provider | Console URL | Callback URI | Scopes |
|---|---|---|---|
| Google | https://console.cloud.google.com/apis/credentials | `/api/v1/auth/oauth/google/callback` | `openid email profile` |
| GitHub | https://github.com/settings/developers | `/api/v1/auth/oauth/github/callback` | `read:user user:email` |
| Microsoft | https://entra.microsoft.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade | `/api/v1/auth/oauth/microsoft/callback` | `openid email profile offline_access` |
| Apple | https://developer.apple.com/account/resources/identifiers/list | `/api/v1/auth/oauth/apple/callback` | `name email` |
| Discord | https://discord.com/developers/applications | `/api/v1/auth/oauth/discord/callback` | `identify email` |
| GitLab | https://gitlab.com/-/profile/applications | `/api/v1/auth/oauth/gitlab/callback` | `read_user openid email profile` |
| Bitbucket | https://bitbucket.org/account/settings/api | `/api/v1/auth/oauth/bitbucket/callback` | `account email` |
| Slack | https://api.slack.com/apps | `/api/v1/auth/oauth/slack/callback` | `openid email profile` |
| Notion | https://www.notion.com/my-integrations | `/api/v1/auth/oauth/notion/callback` | none; Notion uses workspace/page permissions |
| Salesforce | https://login.salesforce.com | `/api/v1/auth/oauth/salesforce/callback` | `id email profile openid` |
| HubSpot | https://app.hubspot.com/developer | `/api/v1/auth/oauth/hubspot/callback` | `oauth crm.objects.contacts.read` |

## Google

Docs: https://developers.google.com/identity/protocols/oauth2/web-server

1. Open Google Cloud Console -> APIs & Services -> OAuth consent screen.
2. Create or select the project that owns the OmniSight login app.
3. Configure the consent screen:
   - App name: `OmniSight`
   - User support email: operator support address
   - Authorized domains: the OmniSight public domain
4. Open APIs & Services -> Credentials -> Create credentials -> OAuth client ID.
5. Application type: `Web application`.
6. Add an authorized redirect URI:
   `https://<base>/api/v1/auth/oauth/google/callback`.
7. Save the client.
8. Copy the Client ID to `OMNISIGHT_OAUTH_GOOGLE_CLIENT_ID`.
9. Copy the Client secret to `OMNISIGHT_OAUTH_GOOGLE_CLIENT_SECRET`.
10. In OmniSight, request scopes: `openid email profile`.

Notes:

- Google requires the authorization request `redirect_uri` to exactly match an
  authorized redirect URI, including scheme, host, path, case, and trailing
  slash.
- OmniSight already sends `access_type=offline` and `prompt=consent`.

## GitHub

Docs: https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/creating-an-oauth-app

1. Open GitHub -> Settings -> Developer settings -> OAuth Apps.
2. Click `New OAuth App`.
3. Application name: `OmniSight`.
4. Homepage URL: `https://<base>/`.
5. Authorization callback URL:
   `https://<base>/api/v1/auth/oauth/github/callback`.
6. Register the application.
7. Copy the Client ID to `OMNISIGHT_OAUTH_GITHUB_CLIENT_ID`.
8. Generate a new client secret and copy it to
   `OMNISIGHT_OAUTH_GITHUB_CLIENT_SECRET`.
9. In OmniSight, request scopes: `read:user user:email`.

Notes:

- Classic GitHub OAuth Apps accept one callback URL. Use separate apps for
  production, staging, and local development.
- Organization-owned apps live under organization settings instead of personal
  developer settings.

## Microsoft

Docs:

- https://learn.microsoft.com/en-us/entra/identity-platform/quickstart-register-app
- https://learn.microsoft.com/en-us/entra/identity-platform/how-to-add-redirect-uri

1. Open Microsoft Entra admin center -> Identity -> Applications -> App
   registrations.
2. Click `New registration`.
3. Name: `OmniSight`.
4. Supported account types:
   - Use `Accounts in any organizational directory and personal Microsoft
     accounts` for the default OmniSight `common` endpoint.
   - Use a tenant-restricted option only if this deployment must accept one
     tenant.
5. Redirect URI platform: `Web`.
6. Redirect URI:
   `https://<base>/api/v1/auth/oauth/microsoft/callback`.
7. Register the app.
8. Copy `Application (client) ID` to
   `OMNISIGHT_OAUTH_MICROSOFT_CLIENT_ID`.
9. Open Certificates & secrets -> Client secrets -> New client secret.
10. Copy the secret value to `OMNISIGHT_OAUTH_MICROSOFT_CLIENT_SECRET`.
11. In OmniSight, request scopes: `openid email profile offline_access`.

Notes:

- The default backend endpoints use
  `https://login.microsoftonline.com/common/oauth2/v2.0/...`.
- If a deployment requires a tenant-specific endpoint, that is a code/config
  extension outside this setup doc.

## Apple

Docs:

- https://developer.apple.com/help/account/capabilities/configure-sign-in-with-apple-for-the-web
- https://developer.apple.com/documentation/signinwithapple/configuring-your-environment-for-sign-in-with-apple

1. Open Apple Developer -> Certificates, Identifiers & Profiles.
2. Confirm an App ID exists with `Sign in with Apple` enabled.
3. Create a `Services ID` for OmniSight web login.
4. Enable `Sign in with Apple` on the Services ID.
5. Associate the Services ID with the primary App ID.
6. Under Website URLs, add the OmniSight domain.
7. Under Return URLs, add:
   `https://<base>/api/v1/auth/oauth/apple/callback`.
8. Save the Services ID.
9. Create a Sign in with Apple private key under Keys.
10. Record Team ID, Key ID, Services ID, and the private key.
11. Generate the Apple client secret JWT using the Services ID as the client
    identifier.
12. Set `OMNISIGHT_OAUTH_APPLE_CLIENT_ID` to the Services ID.
13. Set `OMNISIGHT_OAUTH_APPLE_CLIENT_SECRET` to the generated JWT.
14. In OmniSight, request scopes: `name email`.

Notes:

- Apple posts the callback when `name` is requested; OmniSight's callback path
  is built to handle the vendor quirk.
- Apple only returns the user's name on the first authorization.

## Discord

Docs: https://discord.com/developers/docs/topics/oauth2

1. Open Discord Developer Portal -> Applications.
2. Click `New Application`.
3. Name: `OmniSight`.
4. Open OAuth2 -> General.
5. Add redirect:
   `https://<base>/api/v1/auth/oauth/discord/callback`.
6. Save changes.
7. Copy Client ID to `OMNISIGHT_OAUTH_DISCORD_CLIENT_ID`.
8. Copy Client Secret to `OMNISIGHT_OAUTH_DISCORD_CLIENT_SECRET`.
9. In OmniSight, request scopes: `identify email`.

Notes:

- `identify` returns the Discord user ID and profile fields.
- `email` is required for OmniSight account linking and login identity.

## GitLab

Docs: https://docs.gitlab.com/integration/oauth_provider/

1. Open GitLab -> Preferences -> Applications.
2. Create a new application.
3. Name: `OmniSight`.
4. Redirect URI:
   `https://<base>/api/v1/auth/oauth/gitlab/callback`.
5. Select scopes:
   - `read_user`
   - `openid`
   - `email`
   - `profile`
6. Save the application.
7. Copy Application ID to `OMNISIGHT_OAUTH_GITLAB_CLIENT_ID`.
8. Copy Secret to `OMNISIGHT_OAUTH_GITLAB_CLIENT_SECRET`.

Notes:

- GitLab.com uses the backend's default endpoints.
- Self-managed GitLab hosts need endpoint override support before they can use
  this operator path.

## Bitbucket

Docs: https://developer.atlassian.com/cloud/bitbucket/oauth-2/

1. Open Bitbucket -> Personal settings or Workspace settings -> OAuth
   consumers.
2. Add a consumer.
3. Name: `OmniSight`.
4. Callback URL:
   `https://<base>/api/v1/auth/oauth/bitbucket/callback`.
5. Select permissions:
   - Account: `Read`
   - Email: `Read`
6. Save the consumer.
7. Copy Key to `OMNISIGHT_OAUTH_BITBUCKET_CLIENT_ID`.
8. Copy Secret to `OMNISIGHT_OAUTH_BITBUCKET_CLIENT_SECRET`.
9. In OmniSight, request scopes: `account email`.

Notes:

- Bitbucket scopes are configured on the consumer. OmniSight also sends the
  scope list so tests and consent behavior remain explicit.
- OmniSight fetches `/2.0/user` and then `/2.0/user/emails` because Bitbucket
  does not include the primary email in the user profile response.

## Slack

Docs: https://api.slack.com/authentication/sign-in-with-slack

1. Open Slack API -> Your Apps.
2. Click `Create New App`.
3. Choose `From scratch`.
4. App name: `OmniSight`.
5. Select the development workspace.
6. Open OAuth & Permissions.
7. Add Redirect URL:
   `https://<base>/api/v1/auth/oauth/slack/callback`.
8. Save URLs.
9. Open Basic Information.
10. Copy Client ID to `OMNISIGHT_OAUTH_SLACK_CLIENT_ID`.
11. Copy Client Secret to `OMNISIGHT_OAUTH_SLACK_CLIENT_SECRET`.
12. In OmniSight, request OpenID scopes: `openid email profile`.

Notes:

- OmniSight uses Slack's Sign in with Slack OIDC endpoints, not the legacy
  `identity.*` flow.
- The backend calls `openid.connect.userInfo` after token exchange.

## Notion

Docs: https://developers.notion.com/guides/get-started/authorization

1. Open Notion integrations dashboard.
2. Create a new integration or public connection for OmniSight.
3. Set the integration type to public/OAuth.
4. Add Redirect URI:
   `https://<base>/api/v1/auth/oauth/notion/callback`.
5. Configure the integration's user-facing name, logo, company, and website.
6. Configure page/workspace permissions according to the deployment need.
7. Save the integration.
8. Copy OAuth Client ID to `OMNISIGHT_OAUTH_NOTION_CLIENT_ID`.
9. Copy OAuth Client Secret to `OMNISIGHT_OAUTH_NOTION_CLIENT_SECRET`.
10. Do not add OAuth scopes in OmniSight; Notion uses workspace/page
    permissions and the backend sends no `scope` parameter.

Notes:

- OmniSight sends `owner=user`.
- Notion returns owner user information in the token response, so OmniSight
  does not perform a separate userinfo call.

## Salesforce

Docs:

- https://help.salesforce.com/s/articleView?id=sf.connected_app_create_api_integration.htm
- https://help.salesforce.com/s/articleView?id=xcloud.remoteaccess_using_userinfo_endpoint.htm

1. Open Salesforce Setup.
2. Search for `App Manager`.
3. Click `New Connected App`.
4. Name: `OmniSight`.
5. Contact email: operator support address.
6. Enable OAuth settings.
7. Callback URL:
   `https://<base>/api/v1/auth/oauth/salesforce/callback`.
8. Select OAuth scopes:
   - `Access your basic information (id, profile, email, address, phone)`
   - `OpenID Connect`
   - Add refresh/offline access only if this deployment needs long-lived
     Salesforce sessions beyond OmniSight login.
9. Save the connected app.
10. Copy Consumer Key to `OMNISIGHT_OAUTH_SALESFORCE_CLIENT_ID`.
11. Copy Consumer Secret to `OMNISIGHT_OAUTH_SALESFORCE_CLIENT_SECRET`.
12. In OmniSight, request scopes: `id email profile openid`.

Sandbox/community notes:

- Production default: leave `OMNISIGHT_OAUTH_SALESFORCE_LOGIN_BASE_URL` empty.
- Sandbox: set `OMNISIGHT_OAUTH_SALESFORCE_LOGIN_BASE_URL=https://test.salesforce.com`.
- Community/My Domain: set it to the exact HTTPS login origin for that site.

## HubSpot

Docs: https://developers.hubspot.com/docs/apps/developer-platform/build-apps/authentication/oauth/oauth-quickstart-guide

1. Open HubSpot developer account -> Apps.
2. Create or select the OmniSight app.
3. Open Auth / OAuth settings.
4. Add Redirect URL:
   `https://<base>/api/v1/auth/oauth/hubspot/callback`.
5. Configure required scopes:
   - `oauth`
   - `crm.objects.contacts.read`
6. Save the app.
7. Copy Client ID to `OMNISIGHT_OAUTH_HUBSPOT_CLIENT_ID`.
8. Copy Client Secret to `OMNISIGHT_OAUTH_HUBSPOT_CLIENT_SECRET`.
9. In OmniSight, request scopes: `oauth crm.objects.contacts.read`.

Notes:

- HubSpot production OAuth requires HTTPS redirect URLs.
- OmniSight calls `https://api.hubapi.com/integrations/v1/me` with
  `Authorization: Bearer <token>` to identify the HubSpot user/portal.

## Smoke Test

For each configured provider:

1. Restart backend with the server env vars.
2. Rebuild frontend with the `NEXT_PUBLIC_OMNISIGHT_OAUTH_*_CONFIGURED=true`
   flag.
3. Open `/login`.
4. Confirm the provider button is enabled.
5. Click the provider button.
6. Confirm the browser leaves OmniSight for the provider consent screen.
7. Approve the login.
8. Confirm the provider redirects to:
   `/api/v1/auth/oauth/<provider>/callback`.
9. Confirm OmniSight redirects to `/` or the original `next` path.
10. Confirm Account Settings -> Connected accounts shows the provider identity.

Expected backend failures:

| Symptom | Meaning | Fix |
|---|---|---|
| `provider_not_configured` | Missing server-side client ID or secret | Set both `OMNISIGHT_OAUTH_<PROVIDER>_CLIENT_ID` and `OMNISIGHT_OAUTH_<PROVIDER>_CLIENT_SECRET` |
| `redirect_uri_mismatch` at provider | Vendor callback does not exactly match OmniSight's `redirect_uri` | Copy the full callback URL from the authorize request and register it in the vendor console |
| Login button says `Configure in Settings` | Frontend build does not know the provider is configured | Set `NEXT_PUBLIC_OMNISIGHT_OAUTH_<PROVIDER>_CONFIGURED=true` and rebuild frontend |
| Salesforce userinfo 404 or wrong org | Login base URL points at the wrong Salesforce host | Adjust `OMNISIGHT_OAUTH_SALESFORCE_LOGIN_BASE_URL` |
| Bitbucket login has no email | OAuth consumer lacks Email read permission | Add `email` / Email read permission and reauthorize |

## Related

- `backend/security/oauth_vendors.py` - canonical provider endpoints and scopes
- `backend/security/oauth_login_handler.py` - authorize/callback handler
- `backend/config.py` - `OMNISIGHT_OAUTH_*` settings
- `lib/auth/oauth-providers.ts` - frontend provider catalog and docs links
- `app/settings/account/page.tsx` - Auth providers settings panel
- `backend/tests/test_oauth_login_handler.py` - provider callback contracts
