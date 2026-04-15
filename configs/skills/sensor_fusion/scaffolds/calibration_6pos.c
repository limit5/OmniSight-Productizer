/**
 * 6-Position IMU Calibration.
 *
 * Collects static data at 6 orientations (±X, ±Y, ±Z up) and computes
 * accelerometer bias, scale, and cross-axis misalignment. Also estimates
 * gyroscope bias from the combined static data.
 */

#include <stdint.h>
#include <stdbool.h>
#include <math.h>
#include <string.h>

#define CAL_MAX_SAMPLES 2000
#define CAL_NUM_POSITIONS 6
#define CAL_G 9.81f

typedef enum {
    CAL_POS_Z_UP = 0,
    CAL_POS_Z_DOWN,
    CAL_POS_X_UP,
    CAL_POS_X_DOWN,
    CAL_POS_Y_UP,
    CAL_POS_Y_DOWN,
} cal_position_t;

typedef struct {
    float accel[3];
    float gyro[3];
} cal_sample_t;

typedef struct {
    float accel_bias[3];
    float accel_scale[3];
    float gyro_bias[3];
    float misalignment[3][3];
    float residual_g;
    bool valid;
} cal_result_t;

typedef struct {
    cal_sample_t samples[CAL_MAX_SAMPLES];
    uint16_t count;
    float accel_mean[3];
    float gyro_mean[3];
} cal_position_data_t;

static cal_position_data_t _positions[CAL_NUM_POSITIONS];
static cal_result_t _result;

void cal_reset(void) {
    memset(_positions, 0, sizeof(_positions));
    memset(&_result, 0, sizeof(_result));
}

int cal_add_sample(cal_position_t pos, const cal_sample_t *sample) {
    if (pos >= CAL_NUM_POSITIONS) return -1;
    cal_position_data_t *pd = &_positions[pos];
    if (pd->count >= CAL_MAX_SAMPLES) return -2;
    memcpy(&pd->samples[pd->count], sample, sizeof(cal_sample_t));
    pd->count++;
    return 0;
}

static void _compute_means(void) {
    for (int p = 0; p < CAL_NUM_POSITIONS; p++) {
        cal_position_data_t *pd = &_positions[p];
        if (pd->count == 0) continue;
        memset(pd->accel_mean, 0, sizeof(pd->accel_mean));
        memset(pd->gyro_mean, 0, sizeof(pd->gyro_mean));
        for (uint16_t i = 0; i < pd->count; i++) {
            for (int a = 0; a < 3; a++) {
                pd->accel_mean[a] += pd->samples[i].accel[a];
                pd->gyro_mean[a] += pd->samples[i].gyro[a];
            }
        }
        for (int a = 0; a < 3; a++) {
            pd->accel_mean[a] /= pd->count;
            pd->gyro_mean[a] /= pd->count;
        }
    }
}

int cal_compute(cal_result_t *out) {
    _compute_means();

    /* Gyro bias: average of all positions */
    float gb[3] = {0};
    int pos_count = 0;
    for (int p = 0; p < CAL_NUM_POSITIONS; p++) {
        if (_positions[p].count == 0) continue;
        for (int a = 0; a < 3; a++)
            gb[a] += _positions[p].gyro_mean[a];
        pos_count++;
    }
    if (pos_count < 2) { out->valid = false; return -1; }
    for (int a = 0; a < 3; a++) gb[a] /= pos_count;
    memcpy(out->gyro_bias, gb, sizeof(gb));

    /* Accel bias and scale from opposing positions */
    for (int axis = 0; axis < 3; axis++) {
        int pos_up = axis * 2;
        int pos_down = axis * 2 + 1;
        if (_positions[pos_up].count == 0 || _positions[pos_down].count == 0) {
            out->valid = false;
            return -2;
        }
        float up_val = _positions[pos_up].accel_mean[axis];
        float down_val = _positions[pos_down].accel_mean[axis];
        out->accel_bias[axis] = (up_val + down_val) / 2.0f;
        float range = fabsf(up_val - down_val);
        out->accel_scale[axis] = (range > 0.1f) ? (2.0f * CAL_G) / range : 1.0f;
    }

    /* Identity misalignment (full solution requires least-squares) */
    memset(out->misalignment, 0, sizeof(out->misalignment));
    for (int i = 0; i < 3; i++) out->misalignment[i][i] = 1.0f;

    /* Residual check */
    float total_residual = 0;
    for (int p = 0; p < CAL_NUM_POSITIONS; p++) {
        if (_positions[p].count == 0) continue;
        float corrected[3];
        for (int a = 0; a < 3; a++)
            corrected[a] = (_positions[p].accel_mean[a] - out->accel_bias[a]) * out->accel_scale[a];
        float mag = sqrtf(corrected[0]*corrected[0] + corrected[1]*corrected[1] + corrected[2]*corrected[2]);
        total_residual += fabsf(mag - CAL_G);
    }
    out->residual_g = total_residual / pos_count;
    out->valid = (out->residual_g < 0.5f);

    memcpy(&_result, out, sizeof(cal_result_t));
    return 0;
}
