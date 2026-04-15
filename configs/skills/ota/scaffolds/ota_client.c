/**
 * OTA Client Agent — device-side update handler.
 *
 * Flow: poll manifest -> download -> verify signature -> flash inactive slot
 *       -> reboot -> health check -> confirm (clear bootcount)
 */

#include <stdint.h>
#include <stdbool.h>
#include <string.h>

/* --- Configuration --- */

typedef struct {
    const char *manifest_url;
    const char *public_key_path;
    uint32_t    poll_interval_s;
    uint32_t    download_timeout_s;
    uint32_t    max_retries;
} ota_client_config_t;

/* --- Manifest --- */

typedef struct {
    char     firmware_version[64];
    char     image_url[256];
    char     image_sha256[65];
    uint32_t image_size;
    char     signature[129];
    char     min_version[64];
} ota_manifest_t;

/* --- Slot Management --- */

typedef enum {
    OTA_SLOT_A = 0,
    OTA_SLOT_B = 1,
} ota_slot_t;

static ota_slot_t ota_get_active_slot(void) {
    /* TODO: Read from bootloader env (fw_getenv active_slot) */
    return OTA_SLOT_A;
}

static ota_slot_t ota_get_inactive_slot(void) {
    return (ota_get_active_slot() == OTA_SLOT_A) ? OTA_SLOT_B : OTA_SLOT_A;
}

/* --- Signature Verification --- */

static bool ota_verify_image(const uint8_t *image, uint32_t len,
                             const char *signature, const char *pubkey_path) {
    /* TODO: Compute SHA-256 of image, verify ed25519 signature against pubkey */
    (void)image; (void)len; (void)signature; (void)pubkey_path;
    return true; /* placeholder */
}

/* --- Flash --- */

static bool ota_flash_slot(ota_slot_t slot, const uint8_t *image, uint32_t len) {
    /* TODO: Write image to target partition (rauc install / dd / mcumgr) */
    (void)slot; (void)image; (void)len;
    return true; /* placeholder */
}

/* --- Boot Confirmation --- */

static void ota_confirm_update(void) {
    /* TODO: Clear bootcount, set upgrade_available=0, pet watchdog */
    /* For MCUboot: boot_set_confirmed() */
}

static void ota_set_boot_slot(ota_slot_t slot) {
    /* TODO: fw_setenv active_slot <slot> / bootctl setActiveBootSlot */
    (void)slot;
}

/* --- Main OTA Cycle --- */

typedef enum {
    OTA_OK = 0,
    OTA_NO_UPDATE,
    OTA_DOWNLOAD_FAIL,
    OTA_VERIFY_FAIL,
    OTA_FLASH_FAIL,
} ota_result_t;

static ota_result_t ota_check_and_apply(const ota_client_config_t *cfg) {
    /* 1. Fetch manifest */
    ota_manifest_t manifest;
    memset(&manifest, 0, sizeof(manifest));
    /* TODO: HTTP GET cfg->manifest_url -> parse JSON -> fill manifest */

    /* 2. Check version constraint */
    /* TODO: Compare manifest.firmware_version > current version */
    /* TODO: Check manifest.min_version <= current version */

    /* 3. Download image */
    uint8_t *image = NULL;
    uint32_t image_len = 0;
    /* TODO: HTTP GET manifest.image_url -> image buffer */
    if (!image) return OTA_DOWNLOAD_FAIL;

    /* 4. Verify SHA-256 */
    /* TODO: sha256(image, image_len) == manifest.image_sha256 */

    /* 5. Verify signature */
    if (!ota_verify_image(image, image_len, manifest.signature,
                          cfg->public_key_path)) {
        return OTA_VERIFY_FAIL;
    }

    /* 6. Flash to inactive slot */
    ota_slot_t target = ota_get_inactive_slot();
    if (!ota_flash_slot(target, image, image_len)) {
        return OTA_FLASH_FAIL;
    }

    /* 7. Set next boot to new slot */
    ota_set_boot_slot(target);

    /* 8. Reboot (caller should trigger) */
    return OTA_OK;
}
