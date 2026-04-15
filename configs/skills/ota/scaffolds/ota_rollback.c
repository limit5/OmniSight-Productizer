/**
 * OTA Rollback Handler — boot-time rollback logic.
 *
 * Integrates with bootloader boot count and hardware watchdog to
 * automatically revert to previous slot on repeated boot failures.
 */

#include <stdint.h>
#include <stdbool.h>

/* --- Bootloader Variables --- */

typedef struct {
    uint32_t bootcount;
    uint32_t upgrade_available;
    uint32_t active_slot;          /* 0=A, 1=B */
    uint32_t max_boot_attempts;
} boot_env_t;

static boot_env_t boot_env_read(void) {
    boot_env_t env = {0};
    /* TODO: fw_getenv bootcount / upgrade_available / active_slot */
    env.max_boot_attempts = 3;
    return env;
}

static void boot_env_write(const boot_env_t *env) {
    /* TODO: fw_setenv for each variable */
    (void)env;
}

/* --- Watchdog --- */

static void watchdog_init(uint32_t timeout_s) {
    /* TODO: Open /dev/watchdog, set timeout via WDIOC_SETTIMEOUT */
    (void)timeout_s;
}

static void watchdog_pet(void) {
    /* TODO: write(wdt_fd, "1", 1) or ioctl(WDIOC_KEEPALIVE) */
}

/* --- Health Check --- */

static bool health_check_run(void) {
    /* TODO: Check required services:
     * - systemd target reached (or RTOS main task running)
     * - Network interface up
     * - Application responding on health endpoint
     */
    return true; /* placeholder */
}

/* --- Rollback Decision --- */

typedef enum {
    ROLLBACK_NONE = 0,
    ROLLBACK_REBOOT,
    ROLLBACK_REVERT,
    ROLLBACK_MARK_BAD,
} rollback_action_t;

static rollback_action_t evaluate_rollback(const boot_env_t *env,
                                            bool health_ok) {
    if (env->bootcount >= env->max_boot_attempts) {
        return ROLLBACK_REVERT;
    }
    if (env->upgrade_available && !health_ok) {
        return ROLLBACK_MARK_BAD;
    }
    return ROLLBACK_NONE;
}

/* --- Boot Confirmation --- */

static void confirm_boot(void) {
    boot_env_t env = boot_env_read();
    env.bootcount = 0;
    env.upgrade_available = 0;
    boot_env_write(&env);
    watchdog_pet();
}

/* --- Init Entry Point --- */

void ota_rollback_init(uint32_t watchdog_timeout_s) {
    watchdog_init(watchdog_timeout_s);

    boot_env_t env = boot_env_read();
    rollback_action_t action = evaluate_rollback(&env, health_check_run());

    switch (action) {
    case ROLLBACK_REVERT:
        /* Switch back to previous slot */
        env.active_slot = (env.active_slot == 0) ? 1 : 0;
        env.bootcount = 0;
        env.upgrade_available = 0;
        boot_env_write(&env);
        /* TODO: trigger reboot */
        break;
    case ROLLBACK_MARK_BAD:
        env.upgrade_available = 0;
        boot_env_write(&env);
        /* TODO: trigger reboot to previous slot */
        break;
    case ROLLBACK_NONE:
    default:
        confirm_boot();
        break;
    }
}
