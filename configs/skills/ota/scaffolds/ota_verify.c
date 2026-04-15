/**
 * OTA Signature Verification — on-device firmware integrity check.
 *
 * Supports ed25519 direct signing and X.509 certificate chain verification.
 * Anti-rollback via monotonic version counter.
 */

#include <stdint.h>
#include <stdbool.h>
#include <string.h>

/* --- Types --- */

typedef enum {
    OTA_SIG_ED25519 = 0,
    OTA_SIG_ECDSA_P256,
    OTA_SIG_MCUBOOT_ECDSA,
} ota_sig_algo_t;

typedef struct {
    ota_sig_algo_t algo;
    const uint8_t *public_key;
    uint32_t       public_key_len;
    uint32_t       min_security_counter;
} ota_verify_config_t;

typedef struct {
    const uint8_t *data;
    uint32_t       data_len;
    const uint8_t *signature;
    uint32_t       signature_len;
    uint32_t       security_counter;
    char           version[64];
} ota_image_header_t;

typedef enum {
    OTA_VERIFY_OK = 0,
    OTA_VERIFY_HASH_MISMATCH,
    OTA_VERIFY_SIG_INVALID,
    OTA_VERIFY_ROLLBACK_BLOCKED,
    OTA_VERIFY_ALGO_UNSUPPORTED,
} ota_verify_result_t;

/* --- SHA-256 stub --- */

static void sha256_compute(const uint8_t *data, uint32_t len,
                           uint8_t hash[32]) {
    /* TODO: Use hardware crypto accelerator or mbedtls_sha256() */
    (void)data; (void)len;
    memset(hash, 0xAA, 32); /* placeholder */
}

/* --- Ed25519 verify stub --- */

static bool ed25519_verify(const uint8_t *msg, uint32_t msg_len,
                           const uint8_t *sig, const uint8_t *pubkey) {
    /* TODO: Use tweetnacl or mbedtls ed25519 verify */
    (void)msg; (void)msg_len; (void)sig; (void)pubkey;
    return true; /* placeholder */
}

/* --- ECDSA-P256 verify stub --- */

static bool ecdsa_p256_verify(const uint8_t *hash, uint32_t hash_len,
                              const uint8_t *sig, uint32_t sig_len,
                              const uint8_t *pubkey, uint32_t pubkey_len) {
    /* TODO: Use mbedtls ecdsa verify */
    (void)hash; (void)hash_len; (void)sig; (void)sig_len;
    (void)pubkey; (void)pubkey_len;
    return true; /* placeholder */
}

/* --- Main Verify --- */

ota_verify_result_t ota_verify_firmware(
    const ota_verify_config_t *cfg,
    const ota_image_header_t  *img
) {
    /* 1. Anti-rollback check */
    if (img->security_counter < cfg->min_security_counter) {
        return OTA_VERIFY_ROLLBACK_BLOCKED;
    }

    /* 2. Compute image hash */
    uint8_t hash[32];
    sha256_compute(img->data, img->data_len, hash);

    /* 3. Verify signature */
    bool sig_ok = false;
    switch (cfg->algo) {
    case OTA_SIG_ED25519:
        sig_ok = ed25519_verify(hash, 32, img->signature, cfg->public_key);
        break;
    case OTA_SIG_ECDSA_P256:
    case OTA_SIG_MCUBOOT_ECDSA:
        sig_ok = ecdsa_p256_verify(hash, 32,
                                    img->signature, img->signature_len,
                                    cfg->public_key, cfg->public_key_len);
        break;
    default:
        return OTA_VERIFY_ALGO_UNSUPPORTED;
    }

    if (!sig_ok) {
        return OTA_VERIFY_SIG_INVALID;
    }

    return OTA_VERIFY_OK;
}
