/*
 * secure_boot.c — Secure boot chain verification scaffold
 *
 * L4-CORE-15 Security Stack
 * Verifies bootloader → kernel → rootfs signature chain.
 */

#include <stdint.h>
#include <stdbool.h>
#include <string.h>

/* Platform-specific includes — replace with actual SoC SDK headers */
/* #include "platform_crypto.h" */
/* #include "otp_fuse.h" */

typedef enum {
    BOOT_STAGE_BL1 = 0,
    BOOT_STAGE_BL2,
    BOOT_STAGE_BL31,
    BOOT_STAGE_BL32,
    BOOT_STAGE_BL33,
    BOOT_STAGE_KERNEL,
    BOOT_STAGE_ROOTFS,
    BOOT_STAGE_COUNT,
} boot_stage_t;

typedef enum {
    VERIFY_OK = 0,
    VERIFY_SIG_INVALID,
    VERIFY_HASH_MISMATCH,
    VERIFY_ROLLBACK,
    VERIFY_KEY_NOT_FOUND,
    VERIFY_ERROR,
} verify_result_t;

typedef struct {
    boot_stage_t stage;
    const uint8_t *image;
    uint32_t image_len;
    const uint8_t *signature;
    uint32_t sig_len;
    uint32_t version_counter;
} boot_image_t;

typedef struct {
    boot_stage_t stage;
    verify_result_t result;
    uint32_t elapsed_us;
} stage_verify_result_t;

static stage_verify_result_t s_results[BOOT_STAGE_COUNT];

/*
 * Verify a single boot stage image signature.
 * Replace crypto_verify_signature() with platform HAL call.
 */
static verify_result_t verify_stage_signature(const boot_image_t *img,
                                               const uint8_t *pubkey,
                                               uint32_t pubkey_len)
{
    if (!img || !pubkey || pubkey_len == 0)
        return VERIFY_ERROR;

    if (!img->image || img->image_len == 0)
        return VERIFY_ERROR;

    if (!img->signature || img->sig_len == 0)
        return VERIFY_SIG_INVALID;

    /*
     * TODO: Replace with platform-specific signature verification.
     *
     * Example for ARM TF-A:
     *   int rc = crypto_mod_verify_signature(
     *       img->image, img->image_len,
     *       img->signature, img->sig_len,
     *       pubkey, pubkey_len, CRYPTO_SHA256);
     *   return (rc == 0) ? VERIFY_OK : VERIFY_SIG_INVALID;
     */

    return VERIFY_OK;
}

/*
 * Check anti-rollback counter against OTP-stored minimum version.
 */
static verify_result_t check_rollback_counter(boot_stage_t stage,
                                               uint32_t image_version)
{
    /*
     * TODO: Read OTP counter for this stage.
     *
     * uint32_t min_version = otp_read_counter(stage);
     * if (image_version < min_version)
     *     return VERIFY_ROLLBACK;
     */

    (void)stage;
    (void)image_version;
    return VERIFY_OK;
}

/*
 * Verify the full boot chain from BL2 through rootfs.
 * BL1 (ROM) is assumed verified by hardware.
 */
int secure_boot_verify_chain(const boot_image_t *images, int count,
                              const uint8_t *root_pubkey,
                              uint32_t root_pubkey_len)
{
    int failures = 0;

    memset(s_results, 0, sizeof(s_results));

    for (int i = 0; i < count && i < BOOT_STAGE_COUNT; i++) {
        verify_result_t vr;

        vr = check_rollback_counter(images[i].stage,
                                     images[i].version_counter);
        if (vr != VERIFY_OK) {
            s_results[i].stage = images[i].stage;
            s_results[i].result = vr;
            failures++;
            continue;
        }

        vr = verify_stage_signature(&images[i], root_pubkey,
                                     root_pubkey_len);
        s_results[i].stage = images[i].stage;
        s_results[i].result = vr;

        if (vr != VERIFY_OK)
            failures++;
    }

    return failures;
}

/*
 * Get verification result for a specific boot stage.
 */
const stage_verify_result_t *secure_boot_get_result(boot_stage_t stage)
{
    if (stage < BOOT_STAGE_COUNT)
        return &s_results[stage];
    return NULL;
}
