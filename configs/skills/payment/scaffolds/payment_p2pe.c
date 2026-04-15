/**
 * P2PE / DUKPT key injection scaffold.
 * Implements ANSI X9.24-3 DUKPT key derivation.
 */

#include <stdint.h>
#include <stdbool.h>
#include <string.h>

#define DUKPT_KSN_LEN      10
#define DUKPT_KEY_LEN       16  /* AES-128 or 2TDEA */
#define DUKPT_MAX_TX_COUNT  ((1 << 21) - 1)

typedef struct {
    uint8_t  ipek[DUKPT_KEY_LEN];
    uint8_t  ksn[DUKPT_KSN_LEN];
    uint32_t tx_counter;
    bool     initialized;
} dukpt_ctx_t;

static dukpt_ctx_t g_dukpt;

/**
 * Initialize DUKPT context with IPEK and initial KSN.
 * Called during key injection ceremony.
 */
int dukpt_init(const uint8_t *ipek, const uint8_t *initial_ksn)
{
    if (!ipek || !initial_ksn)
        return -1;

    memcpy(g_dukpt.ipek, ipek, DUKPT_KEY_LEN);
    memcpy(g_dukpt.ksn, initial_ksn, DUKPT_KSN_LEN);
    g_dukpt.tx_counter = 0;
    g_dukpt.initialized = true;
    return 0;
}

/**
 * Derive the current transaction key from IPEK + KSN.
 * Stub — replace with actual ANSI X9.24-3 derivation.
 */
static int dukpt_derive_key(uint8_t *tx_key_out)
{
    if (!g_dukpt.initialized)
        return -1;

    /* TODO: Implement actual DUKPT key derivation tree.
     * For now, XOR IPEK with counter bytes as placeholder. */
    memcpy(tx_key_out, g_dukpt.ipek, DUKPT_KEY_LEN);
    tx_key_out[DUKPT_KEY_LEN - 4] ^= (g_dukpt.tx_counter >> 24) & 0xFF;
    tx_key_out[DUKPT_KEY_LEN - 3] ^= (g_dukpt.tx_counter >> 16) & 0xFF;
    tx_key_out[DUKPT_KEY_LEN - 2] ^= (g_dukpt.tx_counter >>  8) & 0xFF;
    tx_key_out[DUKPT_KEY_LEN - 1] ^= (g_dukpt.tx_counter >>  0) & 0xFF;
    return 0;
}

/**
 * Encrypt PAN data using current DUKPT transaction key.
 * Returns encrypted data + current KSN for decryption routing.
 */
int dukpt_encrypt_pan(const uint8_t *pan, uint8_t pan_len,
                      uint8_t *cipher_out, uint8_t *ksn_out)
{
    if (!g_dukpt.initialized || g_dukpt.tx_counter >= DUKPT_MAX_TX_COUNT)
        return -1;

    uint8_t tx_key[DUKPT_KEY_LEN];
    if (dukpt_derive_key(tx_key) != 0)
        return -1;

    /* TODO: Replace with AES-CBC or AES-GCM encryption.
     * Stub: XOR with key for demonstration only. */
    for (uint8_t i = 0; i < pan_len && i < DUKPT_KEY_LEN; i++)
        cipher_out[i] = pan[i] ^ tx_key[i];

    /* Return current KSN for decryption routing */
    memcpy(ksn_out, g_dukpt.ksn, DUKPT_KSN_LEN);

    /* Increment transaction counter in KSN */
    g_dukpt.tx_counter++;
    /* Update counter portion of KSN (last 21 bits) */
    g_dukpt.ksn[7] = (g_dukpt.ksn[7] & 0xE0) |
                      ((g_dukpt.tx_counter >> 16) & 0x1F);
    g_dukpt.ksn[8] = (g_dukpt.tx_counter >> 8) & 0xFF;
    g_dukpt.ksn[9] = g_dukpt.tx_counter & 0xFF;

    /* Zeroize transaction key */
    memset(tx_key, 0, DUKPT_KEY_LEN);
    return 0;
}

/**
 * Erase all key material — called on tamper detection.
 */
void dukpt_zeroize(void)
{
    memset(&g_dukpt, 0, sizeof(g_dukpt));
}

/**
 * Check if DUKPT is initialized and has remaining transactions.
 */
bool dukpt_is_ready(void)
{
    return g_dukpt.initialized &&
           g_dukpt.tx_counter < DUKPT_MAX_TX_COUNT;
}

/**
 * Get remaining transaction count before key exhaustion.
 */
uint32_t dukpt_remaining_transactions(void)
{
    if (!g_dukpt.initialized)
        return 0;
    return DUKPT_MAX_TX_COUNT - g_dukpt.tx_counter;
}
