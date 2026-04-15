/**
 * OmniSight Telemetry SDK — C header.
 * C17 L4-CORE-17 Telemetry backend.
 */

#ifndef OMNISIGHT_TELEMETRY_SDK_H
#define OMNISIGHT_TELEMETRY_SDK_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef enum {
    TEL_EVENT_CRASH_DUMP  = 0,
    TEL_EVENT_USAGE       = 1,
    TEL_EVENT_PERF_METRIC = 2,
} telemetry_event_type_t;

typedef enum {
    TEL_OK              = 0,
    TEL_ERR_NOT_INIT    = -1,
    TEL_ERR_NO_CONSENT  = -2,
    TEL_ERR_QUEUE_FULL  = -3,
    TEL_ERR_NETWORK     = -4,
    TEL_ERR_RATE_LIMIT  = -5,
    TEL_ERR_INVALID     = -6,
} telemetry_status_t;

typedef struct {
    const char *endpoint_url;
    const char *device_id;
    const char *profile;
    bool        opt_in;
    uint32_t    batch_size;
    uint32_t    flush_interval_ms;
    uint32_t    max_queue_size;
    bool        offline_queue_enabled;
} telemetry_config_t;

telemetry_status_t telemetry_init(const telemetry_config_t *cfg);
telemetry_status_t telemetry_shutdown(void);
telemetry_status_t telemetry_send_crash(const char *signal, const char *stack_trace,
                                         const char *extra_json);
telemetry_status_t telemetry_send_usage(const char *event_name, const char *metadata_json);
telemetry_status_t telemetry_send_metric(const char *metric_name, double value,
                                          const char *unit);
telemetry_status_t telemetry_flush(void);
telemetry_status_t telemetry_set_consent(bool opted_in);
uint32_t           telemetry_queue_size(void);
bool               telemetry_is_connected(void);

#ifdef __cplusplus
}
#endif

#endif /* OMNISIGHT_TELEMETRY_SDK_H */
