# SSH Runner — Remote Board Execution

Phase 64-C-SSH. When the host architecture does not match the target
(e.g. x86_64 host building for aarch64 board), the T3 resolver selects
the SSH runner to execute commands on a registered remote board.

## Architecture

```
resolve_t3_runner(target_arch, target_os)
  ├─ LOCAL  (arch matches host)
  ├─ SSH    (registered remote target)    ← this module
  ├─ QEMU  (emulation, future)
  └─ BUNDLE (fallback)
```

The SSH runner:
1. Connects via paramiko (key-based auth only — no passwords)
2. Creates a per-run scratch directory on the remote
3. Syncs workspace files via SFTP
4. Executes commands with timeout + heartbeat monitoring
5. Collects output artifacts back via SFTP
6. Cleans up on disconnect

## Setup

### 1. Generate a dedicated SSH key

```bash
ssh-keygen -t ed25519 -C "omnisight-runner" -f ~/.ssh/id_omnisight -N ""
```

### 2. Deploy the public key to the target board

```bash
ssh-copy-id -i ~/.ssh/id_omnisight.pub root@192.168.1.100
```

### 3. Add the board to known_hosts

```bash
ssh-keyscan -H 192.168.1.100 >> ~/.ssh/known_hosts
```

### 4. Register the target

Create `configs/ssh_credentials.yaml` (see `configs/ssh_credentials.example.yaml`):

```yaml
targets:
  - id: rk3588-evk
    arch: aarch64
    os: linux
    host: "192.168.1.100"
    port: 22
    user: root
    key_path: "~/.ssh/id_omnisight"
    sysroot_path: "/opt/vendor_sysroot"
    scratch_dir: "/tmp/omnisight"
```

Alternatively, add deploy fields to a platform profile in `configs/platforms/`:

```yaml
platform: aarch64
deploy_method: ssh
deploy_target_ip: "192.168.1.100"
deploy_user: root
ssh_key: "~/.ssh/id_omnisight"
```

### 5. Verify

```bash
ssh -i ~/.ssh/id_omnisight root@192.168.1.100 "uname -a"
```

## Security Lockdown

### Key permissions

```bash
chmod 600 ~/.ssh/id_omnisight
chmod 700 ~/.ssh
```

The runner refuses keys with group/other read permissions.

### Host key verification

The runner uses `paramiko.RejectPolicy` — it will refuse connections
to hosts not in `~/.ssh/known_hosts`. This prevents MITM attacks.

### Sysroot read-only mount

If `sysroot_path` is configured, the runner warns when it is not
mounted read-only on the remote. Mount it as:

```bash
mount -o remount,ro /opt/vendor_sysroot
```

### Scratch directory isolation

Each run gets a unique scratch directory under `scratch_dir`:
`/tmp/omnisight/run-<timestamp>`. The runner does not write outside
this directory.

### Network recommendations

- Place the target on an isolated VLAN
- Firewall: allow SSH (port 22) only from the OmniSight host
- Disable password auth on the target: `PasswordAuthentication no`
- Consider `AllowUsers omnisight` in sshd_config

## Configuration

Environment variables (prefix `OMNISIGHT_`):

| Variable | Default | Description |
|---|---|---|
| `SSH_RUNNER_ENABLED` | `true` | Kill-switch for SSH runner |
| `SSH_RUNNER_TIMEOUT` | `300` | Max command execution time (seconds) |
| `SSH_RUNNER_HEARTBEAT_INTERVAL` | `30` | Transport liveness check interval |
| `SSH_RUNNER_MAX_OUTPUT_BYTES` | `10000` | Output truncation cap |
| `SSH_CREDENTIALS_FILE` | (auto) | Path to ssh_credentials.yaml |

## Troubleshooting

### "SSH key not found"
Verify `key_path` in ssh_credentials.yaml points to an existing file.

### "SSH key has group/other read permissions"
Run `chmod 600 <key_path>`.

### Host not in known_hosts
Run `ssh-keyscan -H <host> >> ~/.ssh/known_hosts`.

### Connection refused
Verify sshd is running on the target and the port is correct.

### Timeout
Increase `OMNISIGHT_SSH_RUNNER_TIMEOUT` or check network latency.
