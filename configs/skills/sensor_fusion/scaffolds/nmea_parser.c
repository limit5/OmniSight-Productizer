/**
 * NMEA 0183 Parser — GGA / RMC / GSA / GSV / VTG / GLL sentence parsing.
 *
 * Usage:
 *   nmea_result_t result;
 *   nmea_parse("$GPGGA,123456.00,...*XX", &result);
 */

#include <stdint.h>
#include <stdbool.h>
#include <string.h>
#include <stdlib.h>

#define NMEA_MAX_FIELDS 20
#define NMEA_MAX_SENTENCE_LEN 82

typedef enum {
    NMEA_GGA,
    NMEA_RMC,
    NMEA_GSA,
    NMEA_GSV,
    NMEA_VTG,
    NMEA_GLL,
    NMEA_UNKNOWN,
} nmea_sentence_type_t;

typedef struct {
    double latitude;
    double longitude;
    double altitude_m;
    double speed_knots;
    double course_deg;
    double hdop;
    uint8_t fix_quality;
    uint8_t num_satellites;
    uint8_t hour, minute, second;
    uint8_t day, month;
    uint16_t year;
    bool valid;
} nmea_fix_t;

typedef struct {
    nmea_sentence_type_t type;
    char talker[3];
    nmea_fix_t fix;
    bool checksum_valid;
    char raw[NMEA_MAX_SENTENCE_LEN + 1];
} nmea_result_t;

static uint8_t _compute_checksum(const char *s, int start, int end) {
    uint8_t cs = 0;
    for (int i = start; i < end; i++) cs ^= (uint8_t)s[i];
    return cs;
}

static double _parse_lat_lon(const char *s) {
    if (!s || !*s) return 0.0;
    double raw = atof(s);
    int deg = (int)(raw / 100.0);
    double min = raw - deg * 100.0;
    return deg + min / 60.0;
}

int nmea_parse(const char *sentence, nmea_result_t *result) {
    memset(result, 0, sizeof(nmea_result_t));
    if (!sentence || sentence[0] != '$') return -1;

    strncpy(result->raw, sentence, NMEA_MAX_SENTENCE_LEN);

    /* Checksum validation */
    const char *star = strchr(sentence, '*');
    if (star) {
        uint8_t expected = (uint8_t)strtol(star + 1, NULL, 16);
        uint8_t computed = _compute_checksum(sentence, 1, (int)(star - sentence));
        result->checksum_valid = (expected == computed);
    }

    /* Extract talker ID and sentence type */
    result->talker[0] = sentence[1];
    result->talker[1] = sentence[2];
    result->talker[2] = '\0';

    const char *type_str = sentence + 3;
    if (strncmp(type_str, "GGA,", 4) == 0) result->type = NMEA_GGA;
    else if (strncmp(type_str, "RMC,", 4) == 0) result->type = NMEA_RMC;
    else if (strncmp(type_str, "GSA,", 4) == 0) result->type = NMEA_GSA;
    else if (strncmp(type_str, "GSV,", 4) == 0) result->type = NMEA_GSV;
    else if (strncmp(type_str, "VTG,", 4) == 0) result->type = NMEA_VTG;
    else if (strncmp(type_str, "GLL,", 4) == 0) result->type = NMEA_GLL;
    else result->type = NMEA_UNKNOWN;

    result->fix.valid = result->checksum_valid;
    return 0;
}
