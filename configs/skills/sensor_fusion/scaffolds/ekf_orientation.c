/**
 * EKF 9-DoF Orientation Estimator.
 *
 * State: [q0, q1, q2, q3, gbx, gby, gbz] (quaternion + gyro bias)
 * Prediction: quaternion kinematics from gyroscope
 * Update: gravity reference from accelerometer
 */

#include <stdint.h>
#include <stdbool.h>
#include <math.h>
#include <string.h>

#define EKF_STATE_DIM 7

typedef struct {
    float gyro_noise;
    float gyro_bias_drift;
    float accel_noise;
    float mag_noise;
    float initial_covariance;
} ekf_config_t;

typedef struct {
    float q[4];           /* quaternion [w, x, y, z] */
    float gyro_bias[3];   /* rad/s */
    float P[EKF_STATE_DIM][EKF_STATE_DIM]; /* error covariance */
    float euler[3];       /* roll, pitch, yaw (rad) */
    bool converged;
    uint32_t iterations;
} ekf_state_t;

typedef struct {
    float accel[3];  /* m/s² */
    float gyro[3];   /* rad/s */
    float mag[3];    /* µT (optional) */
    float dt;        /* seconds */
} ekf_input_t;

static ekf_config_t _cfg;
static ekf_state_t _state;

static void _quat_normalize(float q[4]) {
    float norm = sqrtf(q[0]*q[0] + q[1]*q[1] + q[2]*q[2] + q[3]*q[3]);
    if (norm < 1e-10f) { q[0] = 1; q[1] = q[2] = q[3] = 0; return; }
    for (int i = 0; i < 4; i++) q[i] /= norm;
}

static void _quat_to_euler(const float q[4], float euler[3]) {
    float sinr = 2.0f * (q[0]*q[1] + q[2]*q[3]);
    float cosr = 1.0f - 2.0f * (q[1]*q[1] + q[2]*q[2]);
    euler[0] = atan2f(sinr, cosr);

    float sinp = 2.0f * (q[0]*q[2] - q[3]*q[1]);
    if (sinp > 1.0f) sinp = 1.0f;
    if (sinp < -1.0f) sinp = -1.0f;
    euler[1] = asinf(sinp);

    float siny = 2.0f * (q[0]*q[3] + q[1]*q[2]);
    float cosy = 1.0f - 2.0f * (q[2]*q[2] + q[3]*q[3]);
    euler[2] = atan2f(siny, cosy);
}

void ekf_init(const ekf_config_t *cfg) {
    memcpy(&_cfg, cfg, sizeof(ekf_config_t));
    memset(&_state, 0, sizeof(ekf_state_t));
    _state.q[0] = 1.0f;
    for (int i = 0; i < EKF_STATE_DIM; i++)
        _state.P[i][i] = _cfg.initial_covariance;
}

void ekf_predict(const ekf_input_t *input) {
    float wx = input->gyro[0] - _state.gyro_bias[0];
    float wy = input->gyro[1] - _state.gyro_bias[1];
    float wz = input->gyro[2] - _state.gyro_bias[2];
    float dt = input->dt;

    float omega_norm = sqrtf(wx*wx + wy*wy + wz*wz);
    float dq[4];
    if (omega_norm > 1e-10f) {
        float ha = omega_norm * dt / 2.0f;
        float s = sinf(ha) / omega_norm;
        dq[0] = cosf(ha); dq[1] = wx*s; dq[2] = wy*s; dq[3] = wz*s;
    } else {
        dq[0] = 1; dq[1] = dq[2] = dq[3] = 0;
    }

    float q_new[4];
    q_new[0] = _state.q[0]*dq[0] - _state.q[1]*dq[1] - _state.q[2]*dq[2] - _state.q[3]*dq[3];
    q_new[1] = _state.q[0]*dq[1] + _state.q[1]*dq[0] + _state.q[2]*dq[3] - _state.q[3]*dq[2];
    q_new[2] = _state.q[0]*dq[2] - _state.q[1]*dq[3] + _state.q[2]*dq[0] + _state.q[3]*dq[1];
    q_new[3] = _state.q[0]*dq[3] + _state.q[1]*dq[2] - _state.q[2]*dq[1] + _state.q[3]*dq[0];
    memcpy(_state.q, q_new, sizeof(q_new));
    _quat_normalize(_state.q);

    for (int i = 0; i < 4; i++) _state.P[i][i] += _cfg.gyro_noise * dt;
    for (int i = 4; i < 7; i++) _state.P[i][i] += _cfg.gyro_bias_drift * dt;

    _state.iterations++;
}

void ekf_update_accel(const ekf_input_t *input) {
    float a_norm = sqrtf(input->accel[0]*input->accel[0] +
                         input->accel[1]*input->accel[1] +
                         input->accel[2]*input->accel[2]);
    if (a_norm < 7.8f || a_norm > 11.8f) return;

    float q0 = _state.q[0], q1 = _state.q[1];
    float q2 = _state.q[2], q3 = _state.q[3];
    float g = 9.81f;

    float gx_pred = 2.0f * (q1*q3 - q0*q2) * g;
    float gy_pred = 2.0f * (q0*q1 + q2*q3) * g;
    float gz_pred = (q0*q0 - q1*q1 - q2*q2 + q3*q3) * g;

    float ex = input->accel[0] - gx_pred;
    float ey = input->accel[1] - gy_pred;
    float ez = input->accel[2] - gz_pred;

    float alpha = 0.05f;
    _state.q[1] += alpha * (q2*ez - q3*ey);
    _state.q[2] += alpha * (q3*ex - q1*ez);
    _state.q[3] += alpha * (q1*ey - q2*ex);
    _quat_normalize(_state.q);

    _quat_to_euler(_state.q, _state.euler);
}

const ekf_state_t *ekf_get_state(void) {
    return &_state;
}
