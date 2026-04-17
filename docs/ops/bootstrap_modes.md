# Bootstrap — Deploy Modes (L7)

Reference for the three *deploy modes* the first-run wizard dispatches
against when it reaches Step 4 ("Start services"). The mode is detected
at runtime by `backend/deploy_mode.py::detect_deploy_mode()`; the
wizard's **Step 4** launcher
(`POST /api/v1/bootstrap/start-services`) then picks a mode-specific
argv and exec's it. The matching log-tail stream
(`GET /api/v1/bootstrap/service-tick`) follows the same dispatch.

---

## At a glance

| Mode             | Detection signal                                        | Start argv                                                | Tail argv                                            | Privilege gate                       |
| ---------------- | ------------------------------------------------------- | --------------------------------------------------------- | ---------------------------------------------------- | ------------------------------------ |
| `systemd`        | `/run/systemd/system` present **or** `systemctl` on PATH | `sudo -n systemctl start omnisight-backend.service omnisight-frontend.service` | `journalctl -u <unit>… --follow …`                  | K1 sudoers NOPASSWD (see below)      |
| `docker-compose` | `/var/run/docker.sock` mounted **or** `docker` on PATH   | `docker compose -f docker-compose.prod.yml up -d`         | `docker compose -f <file> logs --follow …`           | `docker` group membership             |
| `dev`            | Fallback — no systemd, no docker daemon                  | *(no-op — `uvicorn` / `next dev` are already running)*    | *(informational tick only)*                           | n/a                                   |

`dev` is also chosen when the wizard is itself running **inside** a
container that has *no* docker socket mounted — in that case Step 4
cannot control the host, so the launcher deliberately short-circuits
rather than fail loudly.

---

## Detection

`detect_deploy_mode()` returns a `DeployModeDetection` dataclass with:

- `mode` — the chosen `systemd` / `docker-compose` / `dev`
- `in_docker`, `has_systemd`, `has_docker_socket`, `has_docker_binary`,
  `has_systemctl_binary` — the raw probe results
- `reason` — short human-readable blurb (surfaced in Step 4 tooltip +
  audit row metadata)
- `signals` — per-probe evidence strings (e.g. `"/.dockerenv present"`,
  `"systemctl on PATH (no /run/systemd/system)"`)
- `override_source` — set to `"OMNISIGHT_DEPLOY_MODE"` when the env
  override won

### Decision table

| # | Condition                                           | Mode             |
| - | --------------------------------------------------- | ---------------- |
| 0 | `OMNISIGHT_DEPLOY_MODE` env override                | *(env value)*    |
| 1 | `in_docker` **and** `has_docker_socket`             | `docker-compose` |
| 2 | `in_docker` **and not** `has_docker_socket`         | `dev`            |
| 3 | `has_systemd`                                       | `systemd`        |
| 4 | `has_docker_socket` **or** `has_docker_binary`      | `docker-compose` |
| 5 | *(nothing usable)*                                  | `dev`            |

First match wins. Row 3 (systemd) beats row 4 (docker) intentionally:
if both are available on a bare-metal host, systemd units are the
canonical way to keep the services alive across reboots.

### Overriding the auto-detect

Set the `OMNISIGHT_DEPLOY_MODE` environment variable to one of
`systemd`, `docker-compose`, or `dev` to pin a mode. Unknown values are
ignored with a warning in the backend log — a typo will not silently
coerce the launcher into `dev`.

Use-cases for a pinned override:

- **CI** — force `dev` so the wizard never tries to exec `systemctl` /
  `docker compose` inside a runner.
- **Nested-container** test rigs — force `docker-compose` when the
  wizard runs inside a container that *does* have the host socket
  mounted but `in_docker` heuristics are noisy.
- **Debugging** — pin to a mode to reproduce a specific failure
  without changing the host topology.

---

## Mode: `systemd`

### Start command

```
sudo -n systemctl start omnisight-backend.service omnisight-frontend.service
```

- `-n` keeps sudo **non-interactive**: if the sudoers rule is missing,
  sudo exits immediately instead of blocking on a TTY prompt that
  nobody can answer (the wizard has no console).
- Exit code propagates back as HTTP 502 with `stderr_tail` echoing the
  sudo / systemctl error so Step 4 can show a precise remediation
  hint.

### Required host setup

1. **Unit files** — install the two service units under
   `/etc/systemd/system/`:

   - `omnisight-backend.service` — runs `uvicorn backend.main:app` as
     the `omnisight` user.
   - `omnisight-frontend.service` — runs `next start` (prod build)
     after the backend unit is active.

   Run `systemctl daemon-reload` once after dropping them in.

2. **K1 scoped sudoers** — the `omnisight` user needs NOPASSWD to
   start (and **only** start) these two units. The wizard generates
   the exact grant via
   `backend.routers.bootstrap.generate_sudoers_snippet()`:

   ```
   # /etc/sudoers.d/omnisight-bootstrap
   omnisight ALL=(root) NOPASSWD: /usr/bin/systemctl start omnisight-backend.service, /usr/bin/systemctl start omnisight-frontend.service
   ```

   Install with:

   ```bash
   python -c 'from backend.routers.bootstrap import generate_sudoers_snippet; print(generate_sudoers_snippet())' \
     | sudo tee /etc/sudoers.d/omnisight-bootstrap >/dev/null
   sudo visudo -c -f /etc/sudoers.d/omnisight-bootstrap
   sudo chmod 0440 /etc/sudoers.d/omnisight-bootstrap
   ```

   This mirrors the `omnisight-cloudflared` sudoers pattern already
   shipped by B12 (`backend/cloudflared_service.py::SUDOERS_LINE`), so
   operators who've already wired Cloudflare Tunnel don't learn a
   second convention.

   **Scope is deliberately narrow.** Only `start` is granted — the
   wizard never stops or restarts services. Least privilege per K1.

3. **Optional logging group** — add `omnisight` to `systemd-journal`
   so the Step 4 log stream (`journalctl -u …`) works without sudo.
   On Debian/Ubuntu that is the default for daemon users.

### Troubleshooting

| Symptom                                      | Likely cause                                  | Fix                                                   |
| -------------------------------------------- | --------------------------------------------- | ----------------------------------------------------- |
| `sudo: a password is required`               | sudoers rule missing / wrong principal        | Re-install snippet above                              |
| `Unit omnisight-backend.service not found`   | Unit files not installed or daemon-reload not run | Drop units into `/etc/systemd/system/` + `daemon-reload` |
| Step 4 502 with `stderr_tail` = empty        | sudoers fine, unit fine, but ExecStart failed | Open `journalctl -u omnisight-backend.service` tail   |
| Step 4 504 timeout                           | Unit stuck in `activating`, starts slower than 120s | Simplify unit's `ExecStartPre` or raise `_START_TIMEOUT_SECS` |

---

## Mode: `docker-compose`

### Start command

```
docker compose -f docker-compose.prod.yml up -d
```

Override the compose file per-call via the endpoint body's
`compose_file` field (e.g. `docker-compose.edge.yml` for an ARM edge
node). The default lives in
`backend/routers/bootstrap.py::_DEFAULT_COMPOSE_FILE`.

### Required host setup

1. **`docker` + `docker compose` plugin** on PATH — the launcher uses
   the v2 compose subcommand, not the legacy `docker-compose` binary.
2. **Socket access** — either:
   - `/var/run/docker.sock` readable (compose-in-docker: mount the
     socket into the wizard container), or
   - `omnisight` user in the `docker` group.
3. **Compose file** — `docker-compose.prod.yml` at the repo root (or
   wherever the endpoint body says), defining `backend`, `frontend`,
   and any sidecars (e.g. `cloudflared`).

### Container-in-container detection

When `detect_deploy_mode()` sees `/.dockerenv` *and*
`/var/run/docker.sock`, it returns `docker-compose` rather than
`systemd` — running `systemd` inside a plain container rarely works,
but compose-in-docker is a clean path on hosts that mount the socket.

### Troubleshooting

| Symptom                                       | Likely cause                              | Fix                                                  |
| --------------------------------------------- | ----------------------------------------- | ---------------------------------------------------- |
| `permission denied … /var/run/docker.sock`    | `omnisight` not in `docker` group         | `sudo usermod -aG docker omnisight` + re-login       |
| `docker: 'compose' is not a docker command`   | v2 compose plugin missing                 | Install `docker-compose-plugin` or upgrade Docker    |
| Step 4 502, `docker-compose.prod.yml: no such file` | Compose file missing or wrong CWD         | Set `compose_file` in body or run wizard from repo root |
| Step 4 504 timeout                            | Image pull slower than 120s               | Pre-pull images: `docker compose pull` before start  |

---

## Mode: `dev`

### Start command

*None* — the endpoint returns `{"status": "already_running", "command": []}`
and writes an audit row marking the Step 4 gate green. The wizard's
Step 4 UI shows a "dev mode — services already up under uvicorn /
next dev" informational card.

### When this mode wins

- A developer ran `uvicorn backend.main:app --reload` and
  `npm run dev` by hand; no systemd, no docker on the box.
- The wizard is running inside a container with no mounted socket
  (the container **is** the deployment — nothing to start).
- `OMNISIGHT_DEPLOY_MODE=dev` was set to pin dev for testing.

### What is still validated

Step 4's `/readyz` probe still runs — even in dev mode, the wizard
won't mark Step 4 green until the backend and frontend report ready.
Dev mode only skips the *launch*, not the *ready check*. This keeps
the transition from `dev` to `systemd` / `docker-compose`
honest: if `uvicorn` isn't actually running, Step 4 still fails.

---

## Source of truth

- Detection: `backend/deploy_mode.py::detect_deploy_mode()`
- Start argv builder: `backend/routers/bootstrap.py::_start_command()`
- Start handler: `POST /api/v1/bootstrap/start-services`
  (`backend/routers/bootstrap.py::bootstrap_start_services`)
- Tail argv builder: `backend/routers/bootstrap.py::_tick_command()`
- Tail handler: `GET /api/v1/bootstrap/service-tick`
- Sudoers snippet generator:
  `backend/routers/bootstrap.py::generate_sudoers_snippet()`
- Tests:
  `backend/tests/test_deploy_mode.py`,
  `backend/tests/test_bootstrap_start_services.py`,
  `backend/tests/test_bootstrap_service_tick.py`.

See also:

- `docs/operations/deployment.md` — end-to-end self-host runbook on
  WSL + Cloudflare Tunnel (uses the `systemd` mode).
- `backend/cloudflared_service.py` — the B12 counterpart that
  manages the Cloudflare Tunnel unit; its sudoers line follows the
  same pattern this doc describes for the bootstrap launcher.
