/**
 * u-blox UBX Binary Protocol handler.
 *
 * Sync: 0xB5 0x62 | class | id | len_lo | len_hi | payload | ck_a | ck_b
 * Checksum: Fletcher-8 over class + id + length + payload.
 */

#include <stdint.h>
#include <stdbool.h>
#include <string.h>

#define UBX_SYNC_1 0xB5
#define UBX_SYNC_2 0x62
#define UBX_MAX_PAYLOAD 512

/* Message classes */
#define UBX_CLASS_NAV 0x01
#define UBX_CLASS_RXM 0x02
#define UBX_CLASS_INF 0x04
#define UBX_CLASS_ACK 0x05
#define UBX_CLASS_CFG 0x06
#define UBX_CLASS_MON 0x0A
#define UBX_CLASS_TIM 0x0D

/* NAV message IDs */
#define UBX_NAV_PVT    0x07
#define UBX_NAV_STATUS 0x03

/* ACK message IDs */
#define UBX_ACK_ACK 0x01
#define UBX_ACK_NAK 0x00

/* CFG message IDs */
#define UBX_CFG_PRT  0x00
#define UBX_CFG_RATE 0x08
#define UBX_CFG_NAV5 0x24

typedef struct {
    uint8_t msg_class;
    uint8_t msg_id;
    uint16_t length;
    uint8_t payload[UBX_MAX_PAYLOAD];
    bool valid;
} ubx_message_t;

typedef struct {
    uint32_t iTOW;
    uint16_t year;
    uint8_t month, day, hour, min, sec;
    uint8_t fixType;
    uint8_t numSV;
    int32_t lon;  /* 1e-7 degrees */
    int32_t lat;  /* 1e-7 degrees */
    int32_t height;  /* mm */
    int32_t hMSL;    /* mm */
    uint32_t hAcc;   /* mm */
    int32_t velN, velE, velD;  /* mm/s */
} ubx_nav_pvt_t;

static void _fletcher8(const uint8_t *data, uint16_t len, uint8_t *ck_a, uint8_t *ck_b) {
    *ck_a = 0;
    *ck_b = 0;
    for (uint16_t i = 0; i < len; i++) {
        *ck_a += data[i];
        *ck_b += *ck_a;
    }
}

int ubx_parse(const uint8_t *buf, uint16_t buf_len, ubx_message_t *msg) {
    memset(msg, 0, sizeof(ubx_message_t));
    if (buf_len < 8) return -1;
    if (buf[0] != UBX_SYNC_1 || buf[1] != UBX_SYNC_2) return -2;

    msg->msg_class = buf[2];
    msg->msg_id = buf[3];
    msg->length = buf[4] | ((uint16_t)buf[5] << 8);

    if (msg->length > UBX_MAX_PAYLOAD) return -3;
    if (buf_len < (uint16_t)(6 + msg->length + 2)) return -4;

    memcpy(msg->payload, &buf[6], msg->length);

    uint8_t ck_a, ck_b;
    _fletcher8(&buf[2], 4 + msg->length, &ck_a, &ck_b);
    msg->valid = (ck_a == buf[6 + msg->length] && ck_b == buf[7 + msg->length]);

    return 0;
}

int ubx_build(uint8_t msg_class, uint8_t msg_id, const uint8_t *payload,
              uint16_t payload_len, uint8_t *out, uint16_t out_max) {
    uint16_t total = 6 + payload_len + 2;
    if (out_max < total) return -1;

    out[0] = UBX_SYNC_1;
    out[1] = UBX_SYNC_2;
    out[2] = msg_class;
    out[3] = msg_id;
    out[4] = payload_len & 0xFF;
    out[5] = (payload_len >> 8) & 0xFF;
    if (payload_len > 0) memcpy(&out[6], payload, payload_len);

    uint8_t ck_a, ck_b;
    _fletcher8(&out[2], 4 + payload_len, &ck_a, &ck_b);
    out[6 + payload_len] = ck_a;
    out[7 + payload_len] = ck_b;

    return (int)total;
}
