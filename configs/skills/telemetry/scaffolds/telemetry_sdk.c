/**
 * OmniSight Telemetry SDK — C implementation stub.
 * C17 L4-CORE-17 Telemetry backend.
 */

#include "telemetry_sdk.h"

#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

/* --- Internal state --- */

typedef struct {
    char     event_type[32];
    char     payload[4096];
    uint64_t timestamp_ms;
} queued_event_t;

static struct {
    bool              initialized;
    bool              opted_in;
    bool              connected;
    telemetry_config_t config;
    queued_event_t    *queue;
    uint32_t           queue_head;
    uint32_t           queue_tail;
    uint32_t           queue_count;
} g_tel = {0};

/* --- Signal handler for crash capture --- */

static void _crash_signal_handler(int sig) {
    if (!g_tel.initialized) _exit(128 + sig);
    /* Capture minimal crash dump — real implementation would walk the stack */
    char buf[256];
    snprintf(buf, sizeof(buf), "{\"signal\":%d}", sig);
    telemetry_send_crash(sig == SIGSEGV ? "SIGSEGV" : "SIGABRT",
                         "(stack trace unavailable in stub)", buf);
    telemetry_flush();
    signal(sig, SIG_DFL);
    raise(sig);
}

/* --- Public API --- */

telemetry_status_t telemetry_init(const telemetry_config_t *cfg) {
    if (!cfg || !cfg->endpoint_url || !cfg->device_id)
        return TEL_ERR_INVALID;

    g_tel.config      = *cfg;
    g_tel.opted_in    = cfg->opt_in;
    g_tel.connected   = true;
    g_tel.queue_head  = 0;
    g_tel.queue_tail  = 0;
    g_tel.queue_count = 0;

    uint32_t qs = cfg->max_queue_size ? cfg->max_queue_size : 1000;
    g_tel.queue = (queued_event_t *)calloc(qs, sizeof(queued_event_t));
    if (!g_tel.queue) return TEL_ERR_INVALID;

    g_tel.initialized = true;

    /* Install crash handlers */
    signal(SIGSEGV, _crash_signal_handler);
    signal(SIGABRT, _crash_signal_handler);

    return TEL_OK;
}

telemetry_status_t telemetry_shutdown(void) {
    if (!g_tel.initialized) return TEL_ERR_NOT_INIT;
    telemetry_flush();
    free(g_tel.queue);
    g_tel.queue = NULL;
    g_tel.initialized = false;
    return TEL_OK;
}

telemetry_status_t telemetry_send_crash(const char *sig, const char *stack_trace,
                                         const char *extra_json) {
    if (!g_tel.initialized) return TEL_ERR_NOT_INIT;
    if (!g_tel.opted_in)    return TEL_ERR_NO_CONSENT;

    uint32_t max = g_tel.config.max_queue_size ? g_tel.config.max_queue_size : 1000;
    if (g_tel.queue_count >= max) return TEL_ERR_QUEUE_FULL;

    queued_event_t *ev = &g_tel.queue[g_tel.queue_tail % max];
    strncpy(ev->event_type, "crash_dump", sizeof(ev->event_type) - 1);
    snprintf(ev->payload, sizeof(ev->payload),
             "{\"crash_signal\":\"%s\",\"stack_trace\":\"%s\",\"extra\":%s}",
             sig ? sig : "UNKNOWN",
             stack_trace ? stack_trace : "",
             extra_json ? extra_json : "{}");

    struct timespec ts;
    clock_gettime(CLOCK_REALTIME, &ts);
    ev->timestamp_ms = (uint64_t)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;

    g_tel.queue_tail++;
    g_tel.queue_count++;
    return TEL_OK;
}

telemetry_status_t telemetry_send_usage(const char *event_name, const char *metadata_json) {
    if (!g_tel.initialized) return TEL_ERR_NOT_INIT;
    if (!g_tel.opted_in)    return TEL_ERR_NO_CONSENT;

    uint32_t max = g_tel.config.max_queue_size ? g_tel.config.max_queue_size : 1000;
    if (g_tel.queue_count >= max) return TEL_ERR_QUEUE_FULL;

    queued_event_t *ev = &g_tel.queue[g_tel.queue_tail % max];
    strncpy(ev->event_type, "usage_event", sizeof(ev->event_type) - 1);
    snprintf(ev->payload, sizeof(ev->payload),
             "{\"event_name\":\"%s\",\"metadata\":%s}",
             event_name ? event_name : "",
             metadata_json ? metadata_json : "{}");

    struct timespec ts;
    clock_gettime(CLOCK_REALTIME, &ts);
    ev->timestamp_ms = (uint64_t)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;

    g_tel.queue_tail++;
    g_tel.queue_count++;
    return TEL_OK;
}

telemetry_status_t telemetry_send_metric(const char *metric_name, double value,
                                          const char *unit) {
    if (!g_tel.initialized) return TEL_ERR_NOT_INIT;
    if (!g_tel.opted_in)    return TEL_ERR_NO_CONSENT;

    uint32_t max = g_tel.config.max_queue_size ? g_tel.config.max_queue_size : 1000;
    if (g_tel.queue_count >= max) return TEL_ERR_QUEUE_FULL;

    queued_event_t *ev = &g_tel.queue[g_tel.queue_tail % max];
    strncpy(ev->event_type, "perf_metric", sizeof(ev->event_type) - 1);
    snprintf(ev->payload, sizeof(ev->payload),
             "{\"metric_name\":\"%s\",\"metric_value\":%.6f,\"metric_unit\":\"%s\"}",
             metric_name ? metric_name : "",
             value,
             unit ? unit : "");

    struct timespec ts;
    clock_gettime(CLOCK_REALTIME, &ts);
    ev->timestamp_ms = (uint64_t)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;

    g_tel.queue_tail++;
    g_tel.queue_count++;
    return TEL_OK;
}

telemetry_status_t telemetry_flush(void) {
    if (!g_tel.initialized) return TEL_ERR_NOT_INIT;
    if (!g_tel.connected)   return TEL_ERR_NETWORK;

    /* Stub: in production, batch-POST queued events to endpoint_url */
    g_tel.queue_head  = g_tel.queue_tail;
    g_tel.queue_count = 0;
    return TEL_OK;
}

telemetry_status_t telemetry_set_consent(bool opted_in) {
    if (!g_tel.initialized) return TEL_ERR_NOT_INIT;
    g_tel.opted_in = opted_in;
    return TEL_OK;
}

uint32_t telemetry_queue_size(void) {
    return g_tel.queue_count;
}

bool telemetry_is_connected(void) {
    return g_tel.connected;
}
