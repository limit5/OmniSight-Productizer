# Key Management SOP

> L4-CORE-15 Security Stack — Standard Operating Procedure
> Version: 1.0.0 | Date: 2026-04-15

## 1. Overview

This document defines the procedures for managing cryptographic keys across the
OmniSight product lifecycle. It covers key generation, storage, rotation,
revocation, and destruction for all security domains: secure boot, TEE, remote
attestation, SBOM signing, and code signing.

## 2. Key Inventory

All keys MUST be registered in `docs/security/key-inventory.yaml` with:

| Field              | Description                                      |
|--------------------|--------------------------------------------------|
| `key_id`           | Unique identifier (e.g., `sb-root-2026`)         |
| `purpose`          | What the key signs/protects                      |
| `algorithm`        | RSA-2048 / ECDSA-P256 / Ed25519 / AES-256        |
| `storage_location` | HSM / TPM / SE / Vault / offline-USB              |
| `created_date`     | ISO 8601 date                                     |
| `rotation_date`    | Next scheduled rotation                           |
| `owner`            | Responsible individual or team                    |
| `backup_location`  | Location of key backup (if applicable)            |
| `status`           | active / rotated / revoked / destroyed            |

## 3. Key Hierarchy

```
Root of Trust (OTP fuse / HSM)
├── Secure Boot Signing Key (RSA-2048 / ECDSA-P256)
│   ├── BL2 signing sub-key
│   ├── Kernel signing sub-key
│   └── Rootfs dm-verity signing sub-key
├── TEE TA Signing Key (RSA-2048 / ECDSA-P256)
├── OTA Update Signing Key (Ed25519)
├── SBOM / Artifact Signing Key (Ed25519 via cosign)
├── Attestation Identity Key (TPM EK → AK)
└── TLS / mTLS Certificate (ECDSA-P256 / RSA-2048)
```

## 4. Key Generation

### 4.1 Secure Boot Root Key

```bash
# Generate on air-gapped machine or HSM
openssl ecparam -genkey -name prime256v1 -out sb-root-priv.pem
openssl ec -in sb-root-priv.pem -pubout -out sb-root-pub.pem

# Hash public key for OTP fuse programming
sha256sum sb-root-pub.pem > sb-root-pub.sha256
```

- MUST be generated on air-gapped workstation or HSM
- Private key NEVER leaves the generation environment
- Public key hash is fused into SoC OTP during manufacturing

### 4.2 SBOM Signing Key (cosign)

```bash
# Key pair mode
cosign generate-key-pair --output-key-prefix=sbom-signing

# KMS-backed (recommended for CI/CD)
cosign generate-key-pair --kms awskms:///alias/sbom-signing
```

### 4.3 OTA Update Signing Key

```bash
# Ed25519 key for firmware updates
python3 -c "
from nacl.signing import SigningKey
sk = SigningKey.generate()
open('ota-signing.key', 'wb').write(bytes(sk))
open('ota-signing.pub', 'wb').write(bytes(sk.verify_key))
"
```

### 4.4 TEE TA Signing Key

```bash
# OP-TEE TA signing (follows OP-TEE build system)
openssl genrsa -out ta-signing-key.pem 2048
# Or for ECDSA:
openssl ecparam -genkey -name prime256v1 -out ta-signing-key.pem
```

## 5. Key Storage

| Key Type                | Production Storage      | Dev/Test Storage       |
|-------------------------|-------------------------|------------------------|
| Secure Boot Root        | HSM (FIPS 140-2 L3+)   | File (air-gapped USB) |
| TEE TA Signing          | HSM or Vault            | File (encrypted)       |
| OTA Update Signing      | HSM or Vault            | File (encrypted)       |
| SBOM Signing            | KMS / Vault             | Local key pair         |
| Attestation (EK)        | TPM (non-exportable)    | Software TPM (swtpm)   |
| TLS Certificate         | Vault / ACME            | Self-signed            |

### Storage Requirements

- Production signing keys MUST be stored in HSM (FIPS 140-2 Level 3+) or
  cloud KMS with audit logging enabled
- Private keys MUST NEVER be stored in version control, CI/CD variables,
  or unencrypted filesystems
- Backup copies MUST be stored in geographically separate secure locations
- Access to signing keys MUST require multi-party authorization (M-of-N)

## 6. Key Rotation Schedule

| Key Type                | Rotation Period | Trigger                           |
|-------------------------|-----------------|-----------------------------------|
| Secure Boot Root        | Never*          | Compromise only                   |
| Secure Boot Sub-keys    | 12 months       | Scheduled or compromise           |
| TEE TA Signing          | 12 months       | Scheduled or compromise           |
| OTA Update Signing      | 6 months        | Scheduled or compromise           |
| SBOM Signing            | 6 months        | Scheduled or compromise           |
| TLS Certificate         | 90 days         | Auto-renewal (ACME/Vault)         |
| Attestation AK          | 24 months       | Scheduled or device re-enrollment |

*Root key is fused into OTP — rotation requires new hardware revision.

### 6.1 Rotation Procedure

1. Generate new key pair (see Section 4)
2. Register new key in key inventory with `status: active`
3. Update previous key status to `rotated`
4. Sign transition manifest: old key signs endorsement of new key
5. Deploy new public key to verification endpoints
6. Verify new key works with test artifact
7. Archive old key securely (do NOT destroy immediately — needed for
   verification of previously signed artifacts)

## 7. Key Revocation

### 7.1 Emergency Revocation Procedure

1. **Assess**: Determine scope of compromise (which key, since when)
2. **Revoke**: Add key to Certificate Revocation List (CRL) or transparency log
3. **Rotate**: Generate replacement key immediately (Section 6.1)
4. **Re-sign**: Re-sign all artifacts that were signed with compromised key
5. **Notify**: Issue security advisory to affected customers
6. **Audit**: Full incident review within 72 hours
7. **Update**: Patch all devices via OTA with new verification keys

### 7.2 Revocation Channels

- SBOM: Rekor transparency log entry
- OTA: Revocation list pushed to devices
- TLS: OCSP / CRL distribution point
- Secure Boot: OTP anti-rollback counter increment

## 8. Key Destruction

When a key reaches end-of-life and all artifacts signed by it are either
re-signed or expired:

1. Confirm no active artifacts rely on the key
2. Record destruction in key inventory (`status: destroyed`)
3. HSM: Execute vendor-specific key zeroization
4. File-based: Secure erase (multiple overwrite passes)
5. Backup copies: Destroy at all locations
6. Two authorized personnel MUST witness and sign off on destruction

## 9. Audit and Compliance

- All key operations (generation, use, rotation, destruction) MUST be logged
  to the audit trail (see `backend/audit.py` hash-chain)
- Key inventory MUST be reviewed quarterly
- HSM access logs MUST be reviewed monthly
- Compliance mapping:
  - NIST SP 800-57 (Key Management Recommendations)
  - FIPS 140-2 / 140-3 (Cryptographic Module Validation)
  - PCI-DSS Requirement 3 (Protect Stored Data)
  - IEC 62443-4-2 (Industrial Security)

## 10. Development vs Production

| Aspect            | Development                | Production                  |
|-------------------|----------------------------|-----------------------------|
| Key source        | `openssl` / `cosign`       | HSM / KMS                   |
| Key storage       | Local file (encrypted)     | HSM / Vault                 |
| Signing ceremony  | Single developer           | M-of-N multi-party          |
| Rotation          | Manual, as needed          | Automated + scheduled       |
| Audit             | Git log                    | Audit trail + SIEM          |

Development keys MUST NEVER be used in production. The CI/CD pipeline MUST
reject artifacts signed with development keys when deploying to production
environments.

## 11. Incident Response

If a key compromise is suspected:

1. Activate incident response team
2. Revoke affected key immediately (Section 7.1)
3. Assess blast radius: which devices, which artifacts
4. Engage legal/compliance if customer data at risk
5. Post-incident: update this SOP with lessons learned

## 12. Tooling

| Tool              | Purpose                                   |
|-------------------|-------------------------------------------|
| `openssl`         | Key generation, certificate management    |
| `cosign`          | SBOM and artifact signing                 |
| `rekor-cli`       | Transparency log queries                  |
| `tpm2-tools`      | TPM key management and attestation        |
| `imgtool`         | MCUboot image signing                     |
| `mkimage`         | U-Boot FIT image signing                  |
| `sbsign`          | UEFI Secure Boot signing                  |
| `vault`           | HashiCorp Vault secret management         |

## 13. References

- NIST SP 800-57 Part 1 Rev. 5 — Recommendation for Key Management
- NIST SP 800-130 — Framework for Key Management
- TCG TPM 2.0 Library Specification
- GlobalPlatform TEE Protection Profile
- sigstore.dev — Software supply chain security
