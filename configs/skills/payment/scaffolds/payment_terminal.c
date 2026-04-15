/**
 * Payment terminal scaffold — PCI-PTS + EMV + P2PE integration.
 * Adapt to your target SoC and HSM vendor.
 */

#ifndef PAYMENT_TERMINAL_H
#define PAYMENT_TERMINAL_H

#include <stdint.h>
#include <stdbool.h>

/* ── EMV interface types ─────────────────────────────────────────── */

typedef enum {
    EMV_CONTACT,
    EMV_CONTACTLESS,
    EMV_BOTH,
} emv_interface_t;

typedef enum {
    EMV_TX_APPROVED,
    EMV_TX_DECLINED,
    EMV_TX_ONLINE,
    EMV_TX_ABORTED,
    EMV_TX_ERROR,
} emv_tx_result_t;

typedef struct {
    uint8_t  aid[16];
    uint8_t  aid_len;
    char     label[32];
    uint8_t  priority;
} emv_app_t;

typedef struct {
    uint32_t amount;
    uint16_t currency_code;
    uint8_t  tx_type;
    emv_interface_t interface;
} emv_tx_params_t;

/* ── P2PE / DUKPT types ──────────────────────────────────────────── */

typedef struct {
    uint8_t ksn[10];
    uint8_t ipek[16];
    uint32_t tx_counter;
} dukpt_state_t;

/* ── Tamper detection ────────────────────────────────────────────── */

typedef enum {
    TAMPER_NONE,
    TAMPER_CASE_OPEN,
    TAMPER_VOLTAGE_FAULT,
    TAMPER_TEMPERATURE,
    TAMPER_PROBE_DETECT,
} tamper_event_t;

/* ── API ─────────────────────────────────────────────────────────── */

int payment_init(void);
int payment_shutdown(void);

/* EMV */
int emv_select_application(emv_app_t *apps, int max_apps, int *count);
emv_tx_result_t emv_process_transaction(const emv_tx_params_t *params);

/* P2PE */
int p2pe_init_dukpt(const uint8_t *ipek, const uint8_t *ksn);
int p2pe_encrypt_pan(const uint8_t *pan, uint8_t pan_len,
                     uint8_t *cipher_out, uint8_t *ksn_out);

/* PCI-PTS tamper */
int tamper_register_callback(void (*cb)(tamper_event_t event));
int tamper_erase_keys(void);
bool tamper_is_device_secure(void);

/* HSM (host-side) */
int hsm_connect(const char *host, uint16_t port);
int hsm_generate_key(const char *key_type, const char *algorithm,
                     char *key_id_out, size_t key_id_len);
int hsm_disconnect(void);

#endif /* PAYMENT_TERMINAL_H */
