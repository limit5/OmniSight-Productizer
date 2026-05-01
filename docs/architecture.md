# OmniSight Architecture

> This file mixes hand-written design notes with an auto-generated
> API surface index. The block between the
> `BEGIN AUTO-GENERATED` / `END AUTO-GENERATED` sentinels below is
> rewritten by `backend/self_healing_docs.py` whenever the FastAPI
> route surface changes. Edit anything OUTSIDE the sentinels freely;
> edits inside will be lost on next regeneration.

## Overview

_(hand-written — start here.)_

<!-- BEGIN AUTO-GENERATED: backend/self_healing_docs.py -->

_Last regenerated: 2026-05-01T23:31:37Z by_ `backend/self_healing_docs.py`

## API Surface (auto)

- **Total routes**: 714
- **Total schemas**: 251

### Routes

| Method | Path | Summary |
|---|---|---|
| `GET` | `/` | Root |
| `GET` | `/api/v1/agents` | List Agents |
| `POST` | `/api/v1/agents` | Create Agent |
| `DELETE` | `/api/v1/agents/{agent_id}` | Delete Agent |
| `GET` | `/api/v1/agents/{agent_id}` | Get Agent |
| `PATCH` | `/api/v1/agents/{agent_id}` | Update Agent Status |
| `POST` | `/api/v1/agents/{agent_id}/reset` | Force Reset Agent |
| `POST` | `/api/v1/agents/{agent_id}/unfreeze` | Unfreeze Agent |
| `GET` | `/api/v1/api-keys` | List Keys |
| `POST` | `/api/v1/api-keys` | Create Key |
| `DELETE` | `/api/v1/api-keys/{key_id}` | Delete Key |
| `POST` | `/api/v1/api-keys/{key_id}/enable` | Enable Key |
| `POST` | `/api/v1/api-keys/{key_id}/revoke` | Revoke Key |
| `POST` | `/api/v1/api-keys/{key_id}/rotate` | Rotate Key |
| `PATCH` | `/api/v1/api-keys/{key_id}/scopes` | Update Scopes |
| `GET` | `/api/v1/artifacts` | List Artifacts |
| `DELETE` | `/api/v1/artifacts/{artifact_id}` | Delete Artifact |
| `GET` | `/api/v1/artifacts/{artifact_id}` | Get Artifact |
| `GET` | `/api/v1/artifacts/{artifact_id}/download` | Download Artifact |
| `GET` | `/api/v1/audit` | List Audit |
| `GET` | `/api/v1/audit/verify` | Verify Chain |
| `GET` | `/api/v1/audit/verify-all` | Verify All Chains |
| `POST` | `/api/v1/auth/change-password` | Change Password |
| `POST` | `/api/v1/auth/login` | Login |
| `POST` | `/api/v1/auth/logout` | Logout |
| `POST` | `/api/v1/auth/mfa/backup-codes/regenerate` | Backup Codes Regenerate |
| `GET` | `/api/v1/auth/mfa/backup-codes/status` | Backup Codes Status |
| `POST` | `/api/v1/auth/mfa/challenge` | Mfa Challenge |
| `GET` | `/api/v1/auth/mfa/status` | Mfa Status |
| `POST` | `/api/v1/auth/mfa/totp/confirm` | Totp Confirm |
| `POST` | `/api/v1/auth/mfa/totp/disable` | Totp Disable |
| `POST` | `/api/v1/auth/mfa/totp/enroll` | Totp Enroll |
| `POST` | `/api/v1/auth/mfa/webauthn/challenge/begin` | Webauthn Challenge Begin |
| `POST` | `/api/v1/auth/mfa/webauthn/challenge/complete` | Webauthn Challenge Complete |
| `POST` | `/api/v1/auth/mfa/webauthn/register/begin` | Webauthn Register Begin |
| `POST` | `/api/v1/auth/mfa/webauthn/register/complete` | Webauthn Register Complete |
| `DELETE` | `/api/v1/auth/mfa/webauthn/{mfa_id}` | Webauthn Remove |
| `GET` | `/api/v1/auth/oidc/{provider}` | Oidc Redirect |
| `DELETE` | `/api/v1/auth/sessions` | Revoke All Other Sessions |
| `GET` | `/api/v1/auth/sessions` | List Sessions |
| `GET` | `/api/v1/auth/sessions/presence` | Sessions Presence |
| `DELETE` | `/api/v1/auth/sessions/{token_hint}` | Revoke Session |
| `GET` | `/api/v1/auth/tenants` | User Tenants |
| `GET` | `/api/v1/auth/whoami` | Whoami |
| `GET` | `/api/v1/auto-decisions` | List Auto Decisions |
| `GET` | `/api/v1/barcode/artifacts` | Get Artifacts |
| `GET` | `/api/v1/barcode/decode-modes` | Get Decode Modes |
| `GET` | `/api/v1/barcode/frame-samples` | Get Frame Samples |
| `POST` | `/api/v1/barcode/frame-samples/validate` | Validate Sample |
| `GET` | `/api/v1/barcode/frame-samples/{sample_id}` | Get Frame Sample |
| `POST` | `/api/v1/barcode/gate/validate` | Validate Gate |
| `POST` | `/api/v1/barcode/scan` | Scan |
| `GET` | `/api/v1/barcode/symbologies` | Get Symbologies |
| `POST` | `/api/v1/barcode/symbologies/validate` | Validate Symbology |
| `GET` | `/api/v1/barcode/test-recipes` | Get Test Recipes |
| `POST` | `/api/v1/barcode/test-recipes/run` | Run Recipe |
| `GET` | `/api/v1/barcode/vendors` | Get Vendors |
| `GET` | `/api/v1/barcode/vendors/{vendor_id}/capabilities` | Get Vendor Capabilities |
| `POST` | `/api/v1/bootstrap/admin-password` | Bootstrap Admin Password |
| `POST` | `/api/v1/bootstrap/cf-tunnel-skip` | Bootstrap Cf Tunnel Skip |
| `POST` | `/api/v1/bootstrap/finalize` | Bootstrap Finalize |
| `POST` | `/api/v1/bootstrap/llm-provision` | Bootstrap Llm Provision |
| `GET` | `/api/v1/bootstrap/ollama-detect` | Bootstrap Ollama Detect |
| `POST` | `/api/v1/bootstrap/parallel-health-check` | Bootstrap Parallel Health Check |
| `POST` | `/api/v1/bootstrap/reset` | Bootstrap Reset |
| `GET` | `/api/v1/bootstrap/service-tick` | Bootstrap Service Tick |
| `POST` | `/api/v1/bootstrap/smoke-subset` | Bootstrap Smoke Subset |
| `POST` | `/api/v1/bootstrap/start-services` | Bootstrap Start Services |
| `GET` | `/api/v1/bootstrap/status` | Bootstrap Status |
| `POST` | `/api/v1/bootstrap/wait-ready` | Bootstrap Wait Ready |
| `GET` | `/api/v1/budget-strategy` | Get Budget Strategy |
| `PUT` | `/api/v1/budget-strategy` | Put Budget Strategy |
| `POST` | `/api/v1/chat` | Chat |
| `DELETE` | `/api/v1/chat/history` | Clear History |
| `GET` | `/api/v1/chat/history` | Get History |
| `GET` | `/api/v1/chat/sessions` | List Sessions |
| `PATCH` | `/api/v1/chat/sessions/{session_id}/title` | Rename Session |
| `POST` | `/api/v1/chat/stream` | Chat Stream |
| `POST` | `/api/v1/chatops/inject` | Chatops Inject |
| `GET` | `/api/v1/chatops/mirror` | Chatops Mirror |
| `POST` | `/api/v1/chatops/send` | Chatops Send |
| `GET` | `/api/v1/chatops/status` | Chatops Status |
| `POST` | `/api/v1/chatops/webhook/discord` | Discord Webhook |
| `POST` | `/api/v1/chatops/webhook/line` | Line Webhook |
| `POST` | `/api/v1/chatops/webhook/teams` | Teams Webhook |
| `POST` | `/api/v1/cloudflare/provision` | Provision Tunnel |
| `POST` | `/api/v1/cloudflare/rotate-token` | Rotate Token |
| `GET` | `/api/v1/cloudflare/status` | Tunnel Status |
| `DELETE` | `/api/v1/cloudflare/tunnel` | Teardown Tunnel |
| `POST` | `/api/v1/cloudflare/validate-token` | Validate Token |
| `GET` | `/api/v1/cloudflare/zones` | List Zones |
| `POST` | `/api/v1/compliance/run/{tool_name}` | Run Compliance Test |
| `GET` | `/api/v1/compliance/tools` | List Tools |
| `GET` | `/api/v1/compliance/tools/{name}` | Get Tool |
| `GET` | `/api/v1/connectivity/artifacts` | List Artifacts |
| `POST` | `/api/v1/connectivity/artifacts/generate` | Generate Artifacts |
| `POST` | `/api/v1/connectivity/checklist` | Validate Checklist |
| `POST` | `/api/v1/connectivity/composition/resolve` | Resolve Composition |
| `GET` | `/api/v1/connectivity/composition/rules` | List Composition Rules |
| `GET` | `/api/v1/connectivity/protocols` | List Protocols |
| `GET` | `/api/v1/connectivity/protocols/{protocol_id}` | Get Protocol |
| `GET` | `/api/v1/connectivity/protocols/{protocol_id}/features` | Get Features |
| `GET` | `/api/v1/connectivity/protocols/{protocol_id}/recipes` | Get Recipes |
| `POST` | `/api/v1/connectivity/soc-compat` | Check Soc Compatibility |
| `GET` | `/api/v1/connectivity/sub-skills` | List Sub Skills |
| `GET` | `/api/v1/connectivity/sub-skills/{sub_skill_id}` | Get Sub Skill |
| `POST` | `/api/v1/connectivity/test` | Run Connectivity Test |
| `POST` | `/api/v1/dag` | Submit Dag |
| `GET` | `/api/v1/dag/plans/by-dag/{dag_id}` | List Plan Chain |
| `GET` | `/api/v1/dag/plans/{plan_id}` | Get Plan |
| `GET` | `/api/v1/dag/runs/{run_id}/plan` | Get Plan For Run |
| `POST` | `/api/v1/dag/validate` | Validate Dag |
| `GET` | `/api/v1/dashboard/summary` | Get Dashboard Summary |
| `GET` | `/api/v1/decision-rules` | Get Decision Rules |
| `PUT` | `/api/v1/decision-rules` | Put Decision Rules |
| `POST` | `/api/v1/decision-rules/test` | Test Decision Rules |
| `GET` | `/api/v1/decisions` | List Decisions |
| `POST` | `/api/v1/decisions/bulk-undo` | Bulk Undo |
| `POST` | `/api/v1/decisions/sweep` | Trigger Sweep |
| `GET` | `/api/v1/decisions/{decision_id}` | Get Decision |
| `POST` | `/api/v1/decisions/{decision_id}/approve` | Approve Decision |
| `POST` | `/api/v1/decisions/{decision_id}/reject` | Reject Decision |
| `POST` | `/api/v1/decisions/{decision_id}/undo` | Undo Decision |
| `GET` | `/api/v1/enterprise/artifacts` | Get Artifacts |
| `GET` | `/api/v1/enterprise/artifacts/{artifact_id}` | Get Artifact |
| `GET` | `/api/v1/enterprise/audit/actions` | Get Audit Actions |
| `GET` | `/api/v1/enterprise/audit/config` | Get Audit Config |
| `POST` | `/api/v1/enterprise/audit/query` | Query Audit |
| `POST` | `/api/v1/enterprise/audit/verify` | Verify Audit Chain |
| `POST` | `/api/v1/enterprise/audit/write` | Write Audit Entry |
| `POST` | `/api/v1/enterprise/auth/authenticate` | Authenticate |
| `GET` | `/api/v1/enterprise/auth/providers` | Get Auth Providers |
| `GET` | `/api/v1/enterprise/auth/providers/{provider_id}` | Get Auth Provider |
| `POST` | `/api/v1/enterprise/auth/session` | Create Session |
| `GET` | `/api/v1/enterprise/auth/session-config` | Get Session Config |
| `POST` | `/api/v1/enterprise/auth/session/refresh` | Refresh Session |
| `POST` | `/api/v1/enterprise/auth/session/revoke` | Revoke Session |
| `POST` | `/api/v1/enterprise/auth/session/validate` | Validate Session |
| `POST` | `/api/v1/enterprise/export/execute` | Execute Export |
| `GET` | `/api/v1/enterprise/export/steps` | Get Export Steps |
| `GET` | `/api/v1/enterprise/i18n/bundle/{locale_id}/{namespace}` | Get Locale Bundle |
| `GET` | `/api/v1/enterprise/i18n/config` | Get I18N Config |
| `GET` | `/api/v1/enterprise/i18n/coverage` | Get I18N Coverage |
| `GET` | `/api/v1/enterprise/i18n/locales` | Get Locales |
| `GET` | `/api/v1/enterprise/i18n/locales/{locale_id}` | Get Locale |
| `GET` | `/api/v1/enterprise/i18n/namespaces` | Get Namespaces |
| `POST` | `/api/v1/enterprise/i18n/translate` | Translate Key |
| `POST` | `/api/v1/enterprise/import/execute` | Execute Import |
| `GET` | `/api/v1/enterprise/import/formats` | Get Import Formats |
| `GET` | `/api/v1/enterprise/import/formats/{format_id}` | Get Import Format |
| `POST` | `/api/v1/enterprise/import/preview` | Preview Import |
| `GET` | `/api/v1/enterprise/import/steps` | Get Import Steps |
| `GET` | `/api/v1/enterprise/rbac/check/{role_id}/{permission_id}` | Check Permission |
| `POST` | `/api/v1/enterprise/rbac/enforce` | Enforce Policy |
| `GET` | `/api/v1/enterprise/rbac/permissions` | Get Permissions |
| `GET` | `/api/v1/enterprise/rbac/roles` | Get Roles |
| `GET` | `/api/v1/enterprise/rbac/roles/{role_id}` | Get Role |
| `GET` | `/api/v1/enterprise/rbac/roles/{role_id}/permissions` | Get Role Perms |
| `POST` | `/api/v1/enterprise/reports/export` | Export Report |
| `GET` | `/api/v1/enterprise/reports/export-formats` | Get Export Formats |
| `POST` | `/api/v1/enterprise/reports/generate` | Generate Report |
| `GET` | `/api/v1/enterprise/reports/types` | Get Report Types |
| `GET` | `/api/v1/enterprise/reports/types/{type_id}` | Get Report Type |
| `GET` | `/api/v1/enterprise/tenants` | List Tenants |
| `POST` | `/api/v1/enterprise/tenants` | Create Tenant |
| `GET` | `/api/v1/enterprise/tenants/config` | Get Tenant Config |
| `POST` | `/api/v1/enterprise/tenants/rls` | Apply Rls |
| `GET` | `/api/v1/enterprise/tenants/strategies` | Get Tenant Strategies |
| `DELETE` | `/api/v1/enterprise/tenants/{tenant_id}` | Delete Tenant |
| `GET` | `/api/v1/enterprise/tenants/{tenant_id}` | Get Tenant |
| `PATCH` | `/api/v1/enterprise/tenants/{tenant_id}` | Update Tenant |
| `GET` | `/api/v1/enterprise/test-recipes` | Get Test Recipes |
| `GET` | `/api/v1/enterprise/test-recipes/{recipe_id}` | Get Test Recipe |
| `POST` | `/api/v1/enterprise/test-recipes/{recipe_id}/run` | Run Test Recipe |
| `POST` | `/api/v1/enterprise/validate` | Validate Gate |
| `GET` | `/api/v1/enterprise/workflow/approval-config` | Get Approval Config |
| `GET` | `/api/v1/enterprise/workflow/instances` | List Workflows |
| `POST` | `/api/v1/enterprise/workflow/instances` | Create Workflow |
| `GET` | `/api/v1/enterprise/workflow/instances/{instance_id}` | Get Workflow |
| `POST` | `/api/v1/enterprise/workflow/instances/{instance_id}/approve` | Approve Workflow |
| `POST` | `/api/v1/enterprise/workflow/instances/{instance_id}/cancel` | Cancel Workflow |
| `POST` | `/api/v1/enterprise/workflow/instances/{instance_id}/complete` | Complete Workflow |
| `POST` | `/api/v1/enterprise/workflow/instances/{instance_id}/reject` | Reject Workflow |
| `POST` | `/api/v1/enterprise/workflow/instances/{instance_id}/transition` | Transition Workflow |
| `GET` | `/api/v1/enterprise/workflow/states` | Get Workflow States |
| `GET` | `/api/v1/enterprise/workflow/states/{state_id}` | Get Workflow State |
| `GET` | `/api/v1/entropy/agents` | List Entropy |
| `GET` | `/api/v1/entropy/agents/{agent_id}` | Get Entropy |
| `GET` | `/api/v1/events` | Event Stream |
| `GET` | `/api/v1/events/replay` | Replay Events |
| `GET` | `/api/v1/health` | Health Check |
| `GET` | `/api/v1/healthz` | Healthz |
| `GET` | `/api/v1/hil/plugins` | List Plugins |
| `GET` | `/api/v1/hil/plugins/{name}` | Get Plugin |
| `POST` | `/api/v1/hil/run/{skill_name}` | Run Skill Hil |
| `POST` | `/api/v1/hil/validate/{skill_name}` | Validate Skill Hil |
| `POST` | `/api/v1/hmi/abi-check` | Post Abi Check |
| `GET` | `/api/v1/hmi/abi-matrix` | Get Abi Matrix |
| `POST` | `/api/v1/hmi/binding/generate` | Post Binding |
| `POST` | `/api/v1/hmi/budget-check` | Post Budget Check |
| `GET` | `/api/v1/hmi/components` | Get Components |
| `POST` | `/api/v1/hmi/components/assemble` | Post Assemble |
| `GET` | `/api/v1/hmi/frameworks` | Get Frameworks |
| `POST` | `/api/v1/hmi/generate` | Post Generate |
| `GET` | `/api/v1/hmi/i18n-catalog` | Get I18N Catalog |
| `GET` | `/api/v1/hmi/locales` | Get Locales |
| `GET` | `/api/v1/hmi/platforms` | Get Platforms |
| `POST` | `/api/v1/hmi/security-scan` | Post Security Scan |
| `GET` | `/api/v1/hmi/summary` | Get Summary |
| `GET` | `/api/v1/host/accounting` | Get Accounting |
| `GET` | `/api/v1/host/metrics` | Get Host Metrics |
| `GET` | `/api/v1/host/metrics/me` | Get My Tenant Metrics |
| `GET` | `/api/v1/imaging/artifacts` | Get Artifact Definitions |
| `GET` | `/api/v1/imaging/certs` | Get Imaging Certs |
| `POST` | `/api/v1/imaging/certs/generate` | Generate Certs |
| `GET` | `/api/v1/imaging/color-modes` | Get Color Modes |
| `GET` | `/api/v1/imaging/icc/classes` | Get Icc Classes |
| `POST` | `/api/v1/imaging/icc/embed` | Embed Icc Profile |
| `GET` | `/api/v1/imaging/icc/embedding-formats` | Get Icc Embedding Formats |
| `POST` | `/api/v1/imaging/icc/generate` | Generate Icc Profile |
| `GET` | `/api/v1/imaging/icc/profiles` | Get Icc Profiles |
| `GET` | `/api/v1/imaging/icc/profiles/{profile_id}` | Get Icc Profile |
| `GET` | `/api/v1/imaging/icc/rendering-intents` | Get Rendering Intents |
| `POST` | `/api/v1/imaging/isp/run` | Run Isp Pipeline |
| `GET` | `/api/v1/imaging/isp/stages` | Get Isp Stages |
| `GET` | `/api/v1/imaging/ocr/engines` | Get Ocr Engines |
| `GET` | `/api/v1/imaging/ocr/engines/{engine_id}` | Get Ocr Engine |
| `GET` | `/api/v1/imaging/ocr/preprocessing` | Get Ocr Preprocessing |
| `POST` | `/api/v1/imaging/ocr/run` | Run Ocr |
| `GET` | `/api/v1/imaging/output-formats` | Get Output Formats |
| `GET` | `/api/v1/imaging/sane/api-functions` | Get Sane Api Functions |
| `POST` | `/api/v1/imaging/sane/generate` | Generate Sane Backend |
| `GET` | `/api/v1/imaging/sane/options` | Get Sane Options |
| `GET` | `/api/v1/imaging/sensors` | Get Sensor Types |
| `GET` | `/api/v1/imaging/sensors/{sensor_id}` | Get Sensor Type |
| `GET` | `/api/v1/imaging/socs` | Get Compatible Socs |
| `GET` | `/api/v1/imaging/test-recipes` | Get Test Recipes |
| `POST` | `/api/v1/imaging/test-recipes/{recipe_id}/run` | Run Test Recipe |
| `GET` | `/api/v1/imaging/twain/capabilities` | Get Twain Capabilities |
| `POST` | `/api/v1/imaging/twain/generate` | Generate Twain Driver |
| `GET` | `/api/v1/imaging/twain/states` | Get Twain States |
| `POST` | `/api/v1/imaging/twain/transition` | Twain Transition |
| `POST` | `/api/v1/imaging/validate` | Validate Imaging Gate |
| `POST` | `/api/v1/intent/clarify` | Clarify |
| `POST` | `/api/v1/intent/ingest-repo` | Ingest Repo |
| `POST` | `/api/v1/intent/parse` | Parse |
| `POST` | `/api/v1/intent/upload-docs` | Upload Docs |
| `POST` | `/api/v1/invoke` | Invoke Sync |
| `POST` | `/api/v1/invoke/halt` | Invoke Halt |
| `POST` | `/api/v1/invoke/resume` | Invoke Resume |
| `POST` | `/api/v1/invoke/stream` | Invoke Stream |
| `GET` | `/api/v1/livez` | Livez Prefixed |
| `POST` | `/api/v1/memory/{memory_id}/restore` | Restore Memory |
| `GET` | `/api/v1/metrics` | Get Metrics |
| `GET` | `/api/v1/mobile-compliance/gates` | List Gates |
| `POST` | `/api/v1/mobile-compliance/privacy-label` | Generate Privacy Label |
| `POST` | `/api/v1/mobile-compliance/run` | Run Bundle |
| `GET` | `/api/v1/motion/artifacts` | Get Artifacts |
| `GET` | `/api/v1/motion/axes` | Get Axes |
| `GET` | `/api/v1/motion/drivers` | Get Stepper Drivers |
| `GET` | `/api/v1/motion/endstop-types` | Get Endstop Types |
| `GET` | `/api/v1/motion/gcode-commands` | Get Gcode Commands |
| `GET` | `/api/v1/motion/heaters` | Get Heaters |
| `POST` | `/api/v1/motion/machines` | Create Machine |
| `DELETE` | `/api/v1/motion/machines/{machine_id}` | Delete Machine |
| `GET` | `/api/v1/motion/machines/{machine_id}` | Get Machine Status |
| `POST` | `/api/v1/motion/machines/{machine_id}/estop` | Emergency Stop |
| `POST` | `/api/v1/motion/machines/{machine_id}/execute` | Execute Gcode |
| `POST` | `/api/v1/motion/machines/{machine_id}/load` | Load Gcode |
| `GET` | `/api/v1/motion/recipes` | Get Test Recipes |
| `POST` | `/api/v1/motion/recipes/{recipe_id}/run` | Run Test Recipe |
| `POST` | `/api/v1/motion/validate-gate` | Validate Gate |
| `GET` | `/api/v1/operation-mode` | Get Mode |
| `PUT` | `/api/v1/operation-mode` | Put Mode |
| `GET` | `/api/v1/ops/summary` | Ops Summary |
| `GET` | `/api/v1/orchestration/awaiting-human` | Orchestration Awaiting Human |
| `POST` | `/api/v1/orchestration/queue-tick` | Orchestration Queue Tick |
| `GET` | `/api/v1/orchestration/snapshot` | Orchestration Snapshot |
| `POST` | `/api/v1/orchestrator/check-change-ready` | Check Change Ready Endpoint |
| `POST` | `/api/v1/orchestrator/human-vote` | Human Vote Endpoint |
| `POST` | `/api/v1/orchestrator/intake` | Intake Endpoint |
| `POST` | `/api/v1/orchestrator/merge-conflict` | Merge Conflict Endpoint |
| `POST` | `/api/v1/orchestrator/replan` | Replan Endpoint |
| `GET` | `/api/v1/orchestrator/status` | List Status Endpoint |
| `GET` | `/api/v1/orchestrator/status/{jira_ticket}` | Status Endpoint |
| `GET` | `/api/v1/ota/ab-schemes` | List Ab Schemes |
| `POST` | `/api/v1/ota/ab-schemes/switch` | Switch Slot |
| `GET` | `/api/v1/ota/ab-schemes/{scheme_id}` | Get Ab Scheme |
| `GET` | `/api/v1/ota/artifacts` | List Artifacts |
| `POST` | `/api/v1/ota/artifacts/generate` | Generate Artifacts |
| `GET` | `/api/v1/ota/artifacts/{artifact_id}` | Get Artifact |
| `GET` | `/api/v1/ota/certs` | Get Certs |
| `GET` | `/api/v1/ota/delta-engines` | List Engines |
| `GET` | `/api/v1/ota/delta-engines/{engine_id}` | Get Engine |
| `POST` | `/api/v1/ota/delta/apply` | Delta Apply |
| `POST` | `/api/v1/ota/delta/generate` | Delta Generate |
| `POST` | `/api/v1/ota/firmware/sign` | Sign Fw |
| `POST` | `/api/v1/ota/firmware/verify` | Verify Fw |
| `POST` | `/api/v1/ota/manifest/create` | Create Manifest |
| `POST` | `/api/v1/ota/manifest/validate` | Validate Manifest |
| `GET` | `/api/v1/ota/rollback-policies` | List Policies |
| `GET` | `/api/v1/ota/rollback-policies/{policy_id}` | Get Policy |
| `POST` | `/api/v1/ota/rollback/evaluate` | Eval Rollback |
| `GET` | `/api/v1/ota/rollout-strategies` | List Strategies |
| `GET` | `/api/v1/ota/rollout-strategies/{strategy_id}` | Get Strategy |
| `POST` | `/api/v1/ota/rollout/evaluate` | Eval Rollout |
| `GET` | `/api/v1/ota/signature-schemes` | List Sig Schemes |
| `GET` | `/api/v1/ota/signature-schemes/{scheme_id}` | Get Sig Scheme |
| `POST` | `/api/v1/ota/soc-compat` | Check Soc Compat |
| `GET` | `/api/v1/ota/test/recipes` | List Test Recipes |
| `GET` | `/api/v1/ota/test/recipes/{recipe_id}` | Get Test Recipe |
| `POST` | `/api/v1/ota/test/run` | Run Test |
| `GET` | `/api/v1/payment/artifacts` | List Artifact Definitions |
| `GET` | `/api/v1/payment/artifacts/{artifact_id}` | Get Artifact Definition |
| `GET` | `/api/v1/payment/certs` | List Payment Certs |
| `POST` | `/api/v1/payment/certs/generate` | Generate Cert Artifacts |
| `POST` | `/api/v1/payment/certs/register` | Register Payment Cert |
| `GET` | `/api/v1/payment/emv/levels` | List Emv Levels |
| `GET` | `/api/v1/payment/emv/levels/{level_id}` | Get Emv Level |
| `POST` | `/api/v1/payment/emv/test` | Run Emv Test |
| `POST` | `/api/v1/payment/emv/validate` | Validate Emv |
| `POST` | `/api/v1/payment/hsm/decrypt` | Hsm Decrypt |
| `POST` | `/api/v1/payment/hsm/encrypt` | Hsm Encrypt |
| `POST` | `/api/v1/payment/hsm/generate-key` | Hsm Generate Key |
| `GET` | `/api/v1/payment/hsm/sessions` | List Hsm Sessions |
| `POST` | `/api/v1/payment/hsm/sessions` | Create Hsm Session |
| `DELETE` | `/api/v1/payment/hsm/sessions/{session_id}` | Close Hsm Session |
| `GET` | `/api/v1/payment/hsm/vendors` | List Hsm Vendors |
| `GET` | `/api/v1/payment/hsm/vendors/{vendor_id}` | Get Hsm Vendor |
| `GET` | `/api/v1/payment/p2pe/domains` | List P2Pe Domains |
| `POST` | `/api/v1/payment/p2pe/key-injection` | Run Key Injection |
| `GET` | `/api/v1/payment/pci-dss/levels` | List Pci Dss Levels |
| `GET` | `/api/v1/payment/pci-dss/levels/{level_id}` | Get Pci Dss Level |
| `GET` | `/api/v1/payment/pci-dss/requirements` | List Pci Dss Requirements |
| `GET` | `/api/v1/payment/pci-dss/requirements/{req_id}` | Get Pci Dss Requirement |
| `POST` | `/api/v1/payment/pci-dss/validate` | Validate Pci Dss |
| `GET` | `/api/v1/payment/pci-pts/modules` | List Pci Pts Modules |
| `GET` | `/api/v1/payment/pci-pts/modules/{module_id}` | Get Pci Pts Module |
| `POST` | `/api/v1/payment/pci-pts/validate` | Validate Pci Pts |
| `GET` | `/api/v1/payment/socs` | List Compatible Socs |
| `GET` | `/api/v1/payment/socs/{soc_id}` | Get Compatible Soc |
| `GET` | `/api/v1/payment/test-recipes` | List Test Recipes |
| `POST` | `/api/v1/payment/test-recipes/{recipe_id}/run` | Run Test Recipe |
| `POST` | `/api/v1/pep/breaker/reset` | Pep Breaker Reset |
| `POST` | `/api/v1/pep/decision/{pep_id}` | Pep Decision |
| `GET` | `/api/v1/pep/decisions` | Pep Decisions |
| `GET` | `/api/v1/pep/held` | Pep Held |
| `GET` | `/api/v1/pep/live` | Pep Live |
| `GET` | `/api/v1/pep/policy` | Pep Policy |
| `GET` | `/api/v1/pep/status` | Pep Status |
| `GET` | `/api/v1/power/adc` | List Adc Configs |
| `GET` | `/api/v1/power/adc/{adc_id}` | Get Adc Config |
| `POST` | `/api/v1/power/budget` | Compute Budget |
| `GET` | `/api/v1/power/chemistries` | List Battery Chemistries |
| `GET` | `/api/v1/power/domains` | List Power Domains |
| `GET` | `/api/v1/power/domains/{domain_id}` | Get Power Domain |
| `GET` | `/api/v1/power/features` | List Feature Toggles |
| `POST` | `/api/v1/power/lifetime` | Estimate Lifetime |
| `POST` | `/api/v1/power/profile` | Run Profiling |
| `GET` | `/api/v1/power/sleep-states` | List Sleep States |
| `GET` | `/api/v1/power/sleep-states/{state_id}` | Get Sleep State |
| `POST` | `/api/v1/power/transitions` | Detect Transitions |
| `GET` | `/api/v1/printing/artifacts` | Get Artifact Definitions |
| `GET` | `/api/v1/printing/certs` | Get Certs |
| `POST` | `/api/v1/printing/certs/generate` | Generate Certs |
| `POST` | `/api/v1/printing/color/icc/generate` | Generate Icc |
| `GET` | `/api/v1/printing/color/inks` | Get Ink Sets |
| `GET` | `/api/v1/printing/color/inks/{ink_id}` | Get Ink Set |
| `GET` | `/api/v1/printing/color/papers` | Get Paper Profiles |
| `GET` | `/api/v1/printing/color/papers/{paper_id}` | Get Paper Profile |
| `GET` | `/api/v1/printing/color/rendering-intents` | Get Rendering Intents |
| `POST` | `/api/v1/printing/color/select` | Select Profile |
| `GET` | `/api/v1/printing/color/spaces` | Get Color Spaces |
| `GET` | `/api/v1/printing/cups/backends` | Get Cups Backends |
| `GET` | `/api/v1/printing/cups/backends/{backend_id}` | Get Cups Backend |
| `GET` | `/api/v1/printing/ipp/attributes` | Get Ipp Attributes |
| `GET` | `/api/v1/printing/ipp/job-states` | Get Ipp Job States |
| `GET` | `/api/v1/printing/ipp/jobs` | List Ipp Jobs |
| `POST` | `/api/v1/printing/ipp/jobs` | Submit Ipp Job |
| `GET` | `/api/v1/printing/ipp/jobs/{job_id}` | Get Ipp Job |
| `POST` | `/api/v1/printing/ipp/jobs/{job_id}/cancel` | Cancel Ipp Job |
| `POST` | `/api/v1/printing/ipp/jobs/{job_id}/hold` | Hold Ipp Job |
| `POST` | `/api/v1/printing/ipp/jobs/{job_id}/release` | Release Ipp Job |
| `GET` | `/api/v1/printing/ipp/operations` | Get Ipp Operations |
| `GET` | `/api/v1/printing/ipp/operations/{op_id}` | Get Ipp Operation |
| `GET` | `/api/v1/printing/pdl/ghostscript/devices` | Get Ghostscript Devices |
| `GET` | `/api/v1/printing/pdl/ghostscript/devices/{device_id}` | Get Ghostscript Device |
| `GET` | `/api/v1/printing/pdl/languages` | Get Pdl Languages |
| `GET` | `/api/v1/printing/pdl/languages/{lang_id}` | Get Pdl Language |
| `GET` | `/api/v1/printing/pdl/pcl/commands` | Get Pcl Commands |
| `POST` | `/api/v1/printing/pdl/pcl/generate` | Generate Pcl |
| `POST` | `/api/v1/printing/pdl/ps/generate` | Generate Postscript |
| `GET` | `/api/v1/printing/pdl/ps/operators` | Get Ps Operators |
| `GET` | `/api/v1/printing/pdl/raster-formats` | Get Raster Formats |
| `POST` | `/api/v1/printing/pdl/render` | Render Pdf To Raster |
| `GET` | `/api/v1/printing/queue/config` | Get Spooler Config |
| `GET` | `/api/v1/printing/queue/jobs` | List Queue Jobs |
| `POST` | `/api/v1/printing/queue/jobs` | Enqueue Job |
| `GET` | `/api/v1/printing/queue/jobs/{job_id}` | Get Queue Job |
| `POST` | `/api/v1/printing/queue/jobs/{job_id}/cancel` | Cancel Job |
| `POST` | `/api/v1/printing/queue/jobs/{job_id}/complete` | Complete Job |
| `POST` | `/api/v1/printing/queue/jobs/{job_id}/hold` | Hold Job |
| `POST` | `/api/v1/printing/queue/jobs/{job_id}/release` | Release Job |
| `GET` | `/api/v1/printing/queue/lifecycle` | Get Lifecycle States |
| `GET` | `/api/v1/printing/queue/policies` | Get Queue Policies |
| `GET` | `/api/v1/printing/queue/priorities` | Get Priority Levels |
| `GET` | `/api/v1/printing/socs` | Get Compatible Socs |
| `GET` | `/api/v1/printing/test-recipes` | Get Test Recipes |
| `GET` | `/api/v1/printing/test-recipes/{recipe_id}` | Get Test Recipe |
| `POST` | `/api/v1/printing/test-recipes/{recipe_id}/run` | Run Test Recipe |
| `POST` | `/api/v1/printing/validate` | Validate Gate |
| `GET` | `/api/v1/profile` | Get Profile |
| `PUT` | `/api/v1/profile` | Put Profile |
| `PATCH` | `/api/v1/projects/runs/{project_run_id}` | Patch Project Run |
| `GET` | `/api/v1/projects/{project_id}/report` | Get Report |
| `POST` | `/api/v1/projects/{project_id}/report` | Build Report |
| `GET` | `/api/v1/projects/{project_id}/report.html` | Get Report Html |
| `GET` | `/api/v1/projects/{project_id}/report.pdf` | Get Report Pdf |
| `GET` | `/api/v1/projects/{project_id}/runs` | List Project Runs |
| `GET` | `/api/v1/providers` | Get Providers |
| `GET` | `/api/v1/providers/circuits` | Get Circuit Breakers |
| `POST` | `/api/v1/providers/circuits/reset` | Reset Circuit Breaker |
| `PUT` | `/api/v1/providers/fallback-chain` | Update Fallback Chain |
| `GET` | `/api/v1/providers/health` | Get Provider Health |
| `POST` | `/api/v1/providers/switch` | Switch Provider |
| `GET` | `/api/v1/providers/test` | Test Provider |
| `GET` | `/api/v1/providers/validate/{model_spec}` | Validate Model |
| `GET` | `/api/v1/radio/artifacts` | List Artifacts |
| `POST` | `/api/v1/radio/artifacts/generate` | Generate Artifacts |
| `POST` | `/api/v1/radio/checklist` | Validate Checklist |
| `GET` | `/api/v1/radio/regions` | List Regions |
| `GET` | `/api/v1/radio/regions/{region_id}` | Get Region |
| `GET` | `/api/v1/radio/regions/{region_id}/recipes` | Get Recipes |
| `POST` | `/api/v1/radio/test/emissions` | Run Emissions Test |
| `POST` | `/api/v1/radio/test/sar` | Upload Sar |
| `GET` | `/api/v1/readyz` | Readyz Prefixed |
| `GET` | `/api/v1/realtime/cyclictest/configs` | List Cyclictest Configs |
| `GET` | `/api/v1/realtime/cyclictest/configs/{config_id}` | Get Cyclictest Config |
| `POST` | `/api/v1/realtime/cyclictest/run` | Run Cyclictest |
| `POST` | `/api/v1/realtime/gate/check` | Check Threshold Gate |
| `GET` | `/api/v1/realtime/profiles` | List Rt Profiles |
| `GET` | `/api/v1/realtime/profiles/{profile_id}` | Get Rt Profile |
| `GET` | `/api/v1/realtime/profiles/{profile_id}/kernel-config` | Get Kernel Config |
| `POST` | `/api/v1/realtime/report` | Generate Report |
| `GET` | `/api/v1/realtime/tiers` | List Latency Tiers |
| `GET` | `/api/v1/realtime/tiers/{tier_id}` | Get Latency Tier |
| `POST` | `/api/v1/realtime/trace/capture` | Capture Trace |
| `GET` | `/api/v1/realtime/trace/tools` | List Trace Tools |
| `GET` | `/api/v1/realtime/trace/tools/{tool_id}` | Get Trace Tool |
| `POST` | `/api/v1/report/doc-suite/generate` | Doc Suite Generate |
| `GET` | `/api/v1/report/doc-suite/templates` | Doc Suite Templates |
| `POST` | `/api/v1/report/generate` | Generate |
| `POST` | `/api/v1/report/share` | Share |
| `GET` | `/api/v1/report/share/{report_id}` | Shared Report |
| `GET` | `/api/v1/report/{report_id}` | Get Report |
| `GET` | `/api/v1/report/{report_id}/pdf` | Get Pdf |
| `GET` | `/api/v1/rum/dashboard` | Dashboard |
| `POST` | `/api/v1/rum/errors` | Ingest Error |
| `GET` | `/api/v1/rum/errors/recent` | Errors Recent |
| `GET` | `/api/v1/rum/health` | Rum Health |
| `POST` | `/api/v1/rum/vitals` | Ingest Vital |
| `GET` | `/api/v1/runtime/compression` | Get Compression Stats |
| `GET` | `/api/v1/runtime/debug` | Get Debug State |
| `POST` | `/api/v1/runtime/deploy` | Trigger Deploy |
| `GET` | `/api/v1/runtime/devices` | Get Devices |
| `GET` | `/api/v1/runtime/evk` | Get Evk Status |
| `GET` | `/api/v1/runtime/forecast` | Get Project Forecast |
| `POST` | `/api/v1/runtime/forecast/recompute` | Recompute Project Forecast |
| `POST` | `/api/v1/runtime/git-forge/gerrit/finalize` | Finalize Gerrit Integration |
| `POST` | `/api/v1/runtime/git-forge/gerrit/verify-bot` | Verify Gerrit Merger Bot |
| `POST` | `/api/v1/runtime/git-forge/gerrit/verify-submit-rule` | Verify Gerrit Submit Rule |
| `GET` | `/api/v1/runtime/git-forge/gerrit/webhook-info` | Get Gerrit Webhook Info |
| `POST` | `/api/v1/runtime/git-forge/gerrit/webhook-secret/generate` | Generate Gerrit Webhook Secret |
| `POST` | `/api/v1/runtime/git-forge/jira/webhook-secret/generate` | Generate Jira Webhook Secret |
| `GET` | `/api/v1/runtime/git-forge/ssh-pubkey` | Get Git Forge Ssh Pubkey |
| `POST` | `/api/v1/runtime/git-forge/test-token` | Test Git Forge Token |
| `GET` | `/api/v1/runtime/info` | Get System Info |
| `GET` | `/api/v1/runtime/logs` | Get Logs |
| `GET` | `/api/v1/runtime/model-rules` | Get Available Model Rules |
| `GET` | `/api/v1/runtime/notifications` | Get Notifications |
| `GET` | `/api/v1/runtime/notifications/unread-count` | Unread Count |
| `POST` | `/api/v1/runtime/notifications/{notification_id}/read` | Mark Read |
| `GET` | `/api/v1/runtime/npi` | Get Npi State |
| `PUT` | `/api/v1/runtime/npi` | Update Npi State |
| `PATCH` | `/api/v1/runtime/npi/milestones/{milestone_id}` | Update Npi Milestone |
| `PATCH` | `/api/v1/runtime/npi/phases/{phase_id}` | Update Npi Phase |
| `POST` | `/api/v1/runtime/pipeline/advance` | Advance Pipeline Endpoint |
| `POST` | `/api/v1/runtime/pipeline/start` | Start Pipeline |
| `GET` | `/api/v1/runtime/pipeline/status` | Get Pipeline Status Endpoint |
| `GET` | `/api/v1/runtime/pipeline/timeline` | Get Pipeline Timeline |
| `GET` | `/api/v1/runtime/platform-status` | Get Platform Status |
| `GET` | `/api/v1/runtime/platforms/toolchains` | List Toolchains |
| `POST` | `/api/v1/runtime/release` | Create Release |
| `GET` | `/api/v1/runtime/release/manifest` | Get Release Manifest |
| `GET` | `/api/v1/runtime/release/version` | Get Release Version |
| `GET` | `/api/v1/runtime/repos` | Get Repos |
| `GET` | `/api/v1/runtime/roles` | Get Available Roles |
| `GET` | `/api/v1/runtime/sandbox/capacity` | Get Sandbox Capacity |
| `GET` | `/api/v1/runtime/sandbox/capacity/{tenant_id}` | Get Tenant Capacity |
| `GET` | `/api/v1/runtime/settings` | Get Settings |
| `PUT` | `/api/v1/runtime/settings` | Update Settings |
| `GET` | `/api/v1/runtime/settings/git/token-map` | Get Git Token Map |
| `PUT` | `/api/v1/runtime/settings/git/token-map` | Update Git Token Map |
| `GET` | `/api/v1/runtime/simulations` | List Simulations |
| `POST` | `/api/v1/runtime/simulations` | Trigger Simulation |
| `GET` | `/api/v1/runtime/simulations/{sim_id}` | Get Simulation |
| `GET` | `/api/v1/runtime/spec` | Get Spec |
| `PUT` | `/api/v1/runtime/spec` | Update Spec Field |
| `GET` | `/api/v1/runtime/sse-schema` | Get Sse Schema |
| `GET` | `/api/v1/runtime/status` | Get System Status |
| `POST` | `/api/v1/runtime/test/{integration}` | Test Integration |
| `GET` | `/api/v1/runtime/token-budget` | Get Token Budget |
| `PUT` | `/api/v1/runtime/token-budget` | Update Token Budget |
| `POST` | `/api/v1/runtime/token-budget/reset` | Reset Token Freeze |
| `DELETE` | `/api/v1/runtime/tokens` | Reset Token Usage |
| `GET` | `/api/v1/runtime/tokens` | Get Token Usage |
| `GET` | `/api/v1/runtime/tokens/burn-rate` | Get Token Burn Rate |
| `GET` | `/api/v1/runtime/turns` | Get Turn History |
| `GET` | `/api/v1/runtime/vendor/sdks` | List Vendor Sdks |
| `POST` | `/api/v1/runtime/vendor/sdks` | Create Vendor Sdk |
| `DELETE` | `/api/v1/runtime/vendor/sdks/{platform}` | Delete Vendor Sdk |
| `POST` | `/api/v1/runtime/vendor/sdks/{platform}/install` | Install Vendor Sdk |
| `GET` | `/api/v1/runtime/vendor/sdks/{platform}/validate` | Validate Vendor Sdk |
| `GET` | `/api/v1/safety/artifacts` | List Artifacts |
| `POST` | `/api/v1/safety/check` | Check Compliance |
| `POST` | `/api/v1/safety/check-multi` | Check Multi |
| `GET` | `/api/v1/safety/standards` | List Standards |
| `GET` | `/api/v1/safety/standards/{standard_id}` | Get Standard |
| `GET` | `/api/v1/scratchpad/agents` | List Agents |
| `GET` | `/api/v1/scratchpad/agents/{agent_id}` | Get Summary |
| `GET` | `/api/v1/scratchpad/agents/{agent_id}/archive` | Get Archive |
| `GET` | `/api/v1/scratchpad/agents/{agent_id}/preview` | Get Preview |
| `GET` | `/api/v1/secrets` | List Tenant Secrets |
| `POST` | `/api/v1/secrets` | Create Secret |
| `DELETE` | `/api/v1/secrets/{secret_id}` | Delete Secret Endpoint |
| `PUT` | `/api/v1/secrets/{secret_id}` | Update Secret |
| `GET` | `/api/v1/security/artifacts` | List Artifact Definitions |
| `POST` | `/api/v1/security/artifacts/generate` | Generate Cert Artifacts |
| `GET` | `/api/v1/security/attestation/providers` | List Attestation Providers |
| `GET` | `/api/v1/security/attestation/providers/{provider_id}` | Get Attestation Provider |
| `POST` | `/api/v1/security/attestation/quote` | Generate Attestation Quote |
| `POST` | `/api/v1/security/attestation/verify` | Verify Attestation |
| `GET` | `/api/v1/security/boot-chains` | List Boot Chains |
| `POST` | `/api/v1/security/boot-chains/verify` | Verify Boot Chain |
| `GET` | `/api/v1/security/boot-chains/{chain_id}` | Get Boot Chain |
| `POST` | `/api/v1/security/sbom/sign` | Sign Sbom |
| `GET` | `/api/v1/security/sbom/signers` | List Sbom Signers |
| `GET` | `/api/v1/security/sbom/signers/{tool_id}` | Get Sbom Signer |
| `POST` | `/api/v1/security/soc-compat` | Check Soc Security Support |
| `GET` | `/api/v1/security/tee/bindings` | List Tee Bindings |
| `GET` | `/api/v1/security/tee/bindings/{tee_id}` | Get Tee Binding |
| `POST` | `/api/v1/security/tee/session` | Simulate Tee Session |
| `GET` | `/api/v1/security/test/recipes` | List Security Test Recipes |
| `GET` | `/api/v1/security/test/recipes/domain/{domain}` | Get Recipes By Domain |
| `GET` | `/api/v1/security/test/recipes/{recipe_id}` | Get Security Test Recipe |
| `POST` | `/api/v1/security/test/run` | Run Security Test |
| `GET` | `/api/v1/security/threat-models` | List Threat Models |
| `POST` | `/api/v1/security/threat-models/coverage` | Evaluate Threat Coverage |
| `GET` | `/api/v1/security/threat-models/{class_id}` | Get Threat Model |
| `GET` | `/api/v1/sensor-fusion/artifacts` | List Artifact Definitions |
| `POST` | `/api/v1/sensor-fusion/artifacts/generate` | Generate Artifacts |
| `POST` | `/api/v1/sensor-fusion/barometer/altitude` | Calculate Altitude |
| `GET` | `/api/v1/sensor-fusion/barometer/drivers` | List Barometer Drivers |
| `GET` | `/api/v1/sensor-fusion/barometer/drivers/{driver_id}` | Get Barometer Driver |
| `GET` | `/api/v1/sensor-fusion/calibration/profiles` | List Calibration Profiles |
| `GET` | `/api/v1/sensor-fusion/calibration/profiles/{profile_id}` | Get Calibration Profile |
| `POST` | `/api/v1/sensor-fusion/calibration/run` | Run Calibration |
| `GET` | `/api/v1/sensor-fusion/ekf/profiles` | List Ekf Profiles |
| `GET` | `/api/v1/sensor-fusion/ekf/profiles/{profile_id}` | Get Ekf Profile |
| `POST` | `/api/v1/sensor-fusion/ekf/run` | Run Ekf |
| `POST` | `/api/v1/sensor-fusion/gps/nmea/parse` | Parse Nmea |
| `GET` | `/api/v1/sensor-fusion/gps/protocols` | List Gps Protocols |
| `GET` | `/api/v1/sensor-fusion/gps/protocols/{protocol_id}` | Get Gps Protocol |
| `POST` | `/api/v1/sensor-fusion/gps/ubx/parse` | Parse Ubx |
| `GET` | `/api/v1/sensor-fusion/imu/drivers` | List Imu Drivers |
| `GET` | `/api/v1/sensor-fusion/imu/drivers/{driver_id}` | Get Imu Driver |
| `POST` | `/api/v1/sensor-fusion/soc-compat` | Check Soc Compat |
| `GET` | `/api/v1/sensor-fusion/test/recipes` | List Test Recipes |
| `GET` | `/api/v1/sensor-fusion/test/recipes/{recipe_id}` | Get Test Recipe |
| `POST` | `/api/v1/sensor-fusion/test/run` | Run Sensor Test |
| `POST` | `/api/v1/sensor-fusion/trajectory/evaluate` | Evaluate Trajectory |
| `GET` | `/api/v1/sensor-fusion/trajectory/fixtures` | List Trajectory Fixtures |
| `GET` | `/api/v1/sensor-fusion/trajectory/fixtures/{fixture_id}` | Get Trajectory Fixture |
| `POST` | `/api/v1/skills/install` | Skill Install |
| `GET` | `/api/v1/skills/list` | Skill List |
| `GET` | `/api/v1/skills/pending` | List Pending |
| `DELETE` | `/api/v1/skills/pending/{name}` | Discard |
| `GET` | `/api/v1/skills/pending/{name}` | Read Pending |
| `POST` | `/api/v1/skills/pending/{name}/promote` | Promote |
| `GET` | `/api/v1/skills/registry/{name}` | Skill Detail |
| `POST` | `/api/v1/skills/registry/{name}/validate` | Skill Validate |
| `POST` | `/api/v1/storage/cleanup` | Trigger Cleanup |
| `POST` | `/api/v1/storage/sweep` | Trigger Sweep |
| `GET` | `/api/v1/storage/usage` | Get Storage Usage |
| `GET` | `/api/v1/tasks` | List Tasks |
| `POST` | `/api/v1/tasks` | Create Task |
| `GET` | `/api/v1/tasks/handoffs/recent` | Get Recent Handoffs |
| `DELETE` | `/api/v1/tasks/{task_id}` | Delete Task |
| `GET` | `/api/v1/tasks/{task_id}` | Get Task |
| `PATCH` | `/api/v1/tasks/{task_id}` | Update Task |
| `GET` | `/api/v1/tasks/{task_id}/comments` | Get Task Comments |
| `POST` | `/api/v1/tasks/{task_id}/comments` | Add Task Comment |
| `GET` | `/api/v1/tasks/{task_id}/handoffs` | Get Task Handoffs |
| `GET` | `/api/v1/tasks/{task_id}/transitions` | Get Transitions |
| `GET` | `/api/v1/telemetry/artifacts` | List Artifact Definitions |
| `GET` | `/api/v1/telemetry/artifacts/{artifact_id}` | Get Artifact Definition |
| `GET` | `/api/v1/telemetry/certs` | Get Certs |
| `POST` | `/api/v1/telemetry/certs/generate/{soc_id}` | Generate Certs |
| `GET` | `/api/v1/telemetry/dashboards` | List Dashboards |
| `POST` | `/api/v1/telemetry/dashboards/query` | Query Dashboard Panel |
| `GET` | `/api/v1/telemetry/dashboards/{dashboard_id}` | Get Dashboard |
| `GET` | `/api/v1/telemetry/event-types` | List Event Types |
| `GET` | `/api/v1/telemetry/event-types/{type_id}` | Get Event Type |
| `POST` | `/api/v1/telemetry/ingest` | Ingest Events |
| `POST` | `/api/v1/telemetry/ingest/flush` | Flush Offline Queue |
| `GET` | `/api/v1/telemetry/ingestion/config` | Get Ingestion Config |
| `GET` | `/api/v1/telemetry/privacy/config` | Get Privacy Config |
| `POST` | `/api/v1/telemetry/privacy/consent` | Record Consent |
| `GET` | `/api/v1/telemetry/privacy/consent/{device_id}` | Get Consent |
| `POST` | `/api/v1/telemetry/privacy/redact` | Redact Pii |
| `POST` | `/api/v1/telemetry/retry-queue/add` | Add To Retry Queue |
| `POST` | `/api/v1/telemetry/retry-queue/drain` | Drain Retry Queue |
| `GET` | `/api/v1/telemetry/retry-queue/status` | Get Retry Queue Status |
| `GET` | `/api/v1/telemetry/sdk-profiles` | List Sdk Profiles |
| `GET` | `/api/v1/telemetry/sdk-profiles/{profile_id}` | Get Sdk Profile |
| `GET` | `/api/v1/telemetry/socs` | List Compatible Socs |
| `GET` | `/api/v1/telemetry/socs/{soc_id}` | Check Soc Support |
| `GET` | `/api/v1/telemetry/storage/config` | Get Storage Config |
| `POST` | `/api/v1/telemetry/storage/purge` | Run Retention Purge |
| `GET` | `/api/v1/telemetry/test-recipes` | List Test Recipes |
| `GET` | `/api/v1/telemetry/test-recipes/{recipe_id}` | Get Test Recipe |
| `POST` | `/api/v1/telemetry/test-recipes/{recipe_id}/run` | Run Test |
| `GET` | `/api/v1/tenants/egress` | List Egress Policies |
| `GET` | `/api/v1/tenants/egress/requests` | List All Egress Requests |
| `POST` | `/api/v1/tenants/egress/requests/{rid}/approve` | Approve Egress Request |
| `POST` | `/api/v1/tenants/egress/requests/{rid}/reject` | Reject Egress Request |
| `GET` | `/api/v1/tenants/me/egress` | Get My Egress |
| `GET` | `/api/v1/tenants/me/egress/requests` | List My Egress Requests |
| `POST` | `/api/v1/tenants/me/egress/requests` | Submit My Egress Request |
| `GET` | `/api/v1/tenants/{tid}/egress` | Get Egress |
| `PUT` | `/api/v1/tenants/{tid}/egress` | Put Egress |
| `POST` | `/api/v1/tenants/{tid}/egress/dns-cache/reset` | Reset Dns Cache |
| `GET` | `/api/v1/tools` | List Tools |
| `GET` | `/api/v1/tools/by-agent/{agent_type}` | Tools For Agent |
| `GET` | `/api/v1/user-preferences` | List Preferences |
| `GET` | `/api/v1/user-preferences/{key}` | Get Preference |
| `PUT` | `/api/v1/user-preferences/{key}` | Set Preference |
| `GET` | `/api/v1/user/drafts/{slot_key}` | Get User Draft |
| `PUT` | `/api/v1/user/drafts/{slot_key}` | Put User Draft |
| `GET` | `/api/v1/users` | List Users |
| `POST` | `/api/v1/users` | Create User |
| `PATCH` | `/api/v1/users/{user_id}` | Patch User |
| `POST` | `/api/v1/uvc-gadget/bind` | Bind Udc |
| `GET` | `/api/v1/uvc-gadget/compliance` | Run Compliance |
| `POST` | `/api/v1/uvc-gadget/create` | Create Gadget |
| `GET` | `/api/v1/uvc-gadget/descriptors` | Get Descriptors |
| `POST` | `/api/v1/uvc-gadget/destroy` | Destroy Gadget |
| `GET` | `/api/v1/uvc-gadget/formats` | Get Formats |
| `GET` | `/api/v1/uvc-gadget/resolutions` | Get Resolutions |
| `GET` | `/api/v1/uvc-gadget/status` | Get Status |
| `POST` | `/api/v1/uvc-gadget/still/capture` | Capture Still |
| `POST` | `/api/v1/uvc-gadget/stream/start` | Start Stream |
| `POST` | `/api/v1/uvc-gadget/stream/stop` | Stop Stream |
| `POST` | `/api/v1/uvc-gadget/unbind` | Unbind Udc |
| `POST` | `/api/v1/uvc-gadget/xu` | Xu Set |
| `GET` | `/api/v1/uvc-gadget/xu-controls` | Get Xu Controls |
| `GET` | `/api/v1/uvc-gadget/xu/{selector}` | Xu Get |
| `GET` | `/api/v1/vision/artifacts` | Get Artifacts |
| `GET` | `/api/v1/vision/calibration/methods` | Get Calibration Methods |
| `POST` | `/api/v1/vision/calibration/run` | Run Calibration |
| `POST` | `/api/v1/vision/calibration/stereo` | Run Stereo Calibration |
| `POST` | `/api/v1/vision/cameras/configure-trigger` | Configure Trigger |
| `POST` | `/api/v1/vision/cameras/connect` | Connect Camera |
| `GET` | `/api/v1/vision/cameras/models` | Get Camera Models |
| `POST` | `/api/v1/vision/cameras/set-feature` | Set Feature |
| `POST` | `/api/v1/vision/encoder/create` | Create Encoder |
| `GET` | `/api/v1/vision/encoder/interfaces` | Get Encoder Interfaces |
| `POST` | `/api/v1/vision/gate/validate` | Validate Gate |
| `GET` | `/api/v1/vision/genicam/features` | Get Genicam Features |
| `POST` | `/api/v1/vision/line-scan/compose` | Compose Line Scan |
| `GET` | `/api/v1/vision/line-scan/config` | Get Line Scan Config |
| `GET` | `/api/v1/vision/plc/context` | Get Plc Context |
| `POST` | `/api/v1/vision/plc/read` | Read Plc Register |
| `POST` | `/api/v1/vision/plc/write` | Write Plc Register |
| `GET` | `/api/v1/vision/test-recipes` | Get Test Recipes |
| `POST` | `/api/v1/vision/test-recipes/run` | Run Recipe |
| `GET` | `/api/v1/vision/transports` | Get Transports |
| `GET` | `/api/v1/vision/transports/{transport_id}` | Get Transport |
| `GET` | `/api/v1/vision/trigger-modes` | Get Trigger Modes |
| `POST` | `/api/v1/webhooks/gerrit` | Gerrit Webhook |
| `POST` | `/api/v1/webhooks/github` | Github Webhook |
| `POST` | `/api/v1/webhooks/gitlab` | Gitlab Webhook |
| `POST` | `/api/v1/webhooks/jira` | Jira Webhook |
| `GET` | `/api/v1/workflow/in-flight` | List In Flight |
| `GET` | `/api/v1/workflow/runs` | List Runs |
| `GET` | `/api/v1/workflow/runs/{run_id}` | Replay Run |
| `PATCH` | `/api/v1/workflow/runs/{run_id}` | Update Run |
| `POST` | `/api/v1/workflow/runs/{run_id}/cancel` | Cancel Run |
| `POST` | `/api/v1/workflow/runs/{run_id}/finish` | Finish Run |
| `POST` | `/api/v1/workflow/runs/{run_id}/retry` | Retry Run |
| `GET` | `/api/v1/workspaces` | List All Workspaces |
| `POST` | `/api/v1/workspaces/cleanup/{agent_id}` | Cleanup Workspace |
| `POST` | `/api/v1/workspaces/container/build-image` | Build Agent Image |
| `POST` | `/api/v1/workspaces/container/start/{agent_id}` | Start Agent Container |
| `POST` | `/api/v1/workspaces/container/stop/{agent_id}` | Stop Agent Container |
| `GET` | `/api/v1/workspaces/containers` | List Active Containers |
| `POST` | `/api/v1/workspaces/create-pr/{agent_id}` | Create Pr For Workspace |
| `POST` | `/api/v1/workspaces/finalize/{agent_id}` | Finalize Workspace |
| `GET` | `/api/v1/workspaces/handoff/{task_id}` | Get Task Handoff |
| `POST` | `/api/v1/workspaces/provision` | Provision Workspace |
| `GET` | `/api/v1/workspaces/{agent_id}` | Get Workspace Info |
| `GET` | `/healthz` | Healthz |
| `GET` | `/livez` | Livez |
| `GET` | `/readyz` | Readyz |

<!-- END AUTO-GENERATED: backend/self_healing_docs.py -->


## Notes

_(hand-written — append design context, ADRs, etc.)_
