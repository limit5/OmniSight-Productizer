---
id: L-OP-2768-wsl-shared-netns-port-owner
ticket: OP-2768
title: A LISTEN port with no owner visible to root ss lives in another WSL distro or on Windows
date: 2026-08-13
tags: [wsl, docker, networking, forensics, incident]
---

## Situation

Recreating prod `backend-a` failed with `bind 0.0.0.0:8000: address already in
use`. The listener answered HTTP 404 yet could not be attributed to anything:
every system-docker container, rootless docker, all systemd units and socket
units, cron, sshd, cloudflared and a full-process fd sweep came up empty — and
decisively, even **root** `ss -ltnp 'sport = :8000'` showed the LISTEN row with
an **empty Process column**. Hours were spent enumerating Linux processes that
could not, by construction, be the owner.

## Fix

All WSL2 distros share one utility VM and one network namespace. The socket
belonged to the `docker-desktop` distro: Docker Desktop on **Windows** was
running a Portainer container publishing `0.0.0.0:8000`. Two commands on the
Windows side named it in seconds:

```
netstat -ano | findstr :8000
tasklist /FI "PID eq <pid>"        # -> com.docker.backend.exe
docker ps                          # (Windows shell = Docker Desktop engine) -> portainer
```

Resolution: `docker update --restart=no portainer && docker stop portainer`
on Windows (restart=no so the next Windows boot does not re-take the port).

## Verification

The WSL-side ghost socket vanished the moment the Windows container stopped;
`docker start` of backend-a then bound `:8000` normally and the replica went
healthy (`up{backend-a}==1` after 7 days at 0).

## Generalisation

- Diagnostic rule: on WSL2, a LISTEN row whose Process column is empty **under
  root** means the owner is outside this distro's pid namespace. Go straight to
  Windows `netstat -ano` and the other WSL distros (`wsl -l -v`); do not keep
  enumerating local processes.
- Connection-tracing via `/proc/<pid>/net/tcp` is a false-positive trap here:
  every host-netns process (e.g. a `network_mode: host` container) shows the
  same table, so "found in X's netns view" proves nothing.
- Port collisions across distros are silent and first-come: an outage that
  frees a port (backend-a down 7 days) invites a squatter, which then blocks
  the recovery. Fleet port assignments must treat Windows + all distros as one
  shared namespace.
