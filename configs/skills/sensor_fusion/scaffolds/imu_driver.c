/**
 * IMU Driver — MPU6050 / LSM6DS3 / BMI270 abstraction layer.
 *
 * Usage:
 *   imu_config_t cfg = { .bus = IMU_BUS_I2C, .addr = 0x68, .model = IMU_MPU6050 };
 *   imu_init(&cfg);
 *   imu_sample_t sample;
 *   imu_read(&sample);
 */

#include <stdint.h>
#include <stdbool.h>
#include <string.h>

typedef enum {
    IMU_MPU6050,
    IMU_LSM6DS3,
    IMU_BMI270,
} imu_model_t;

typedef enum {
    IMU_BUS_I2C,
    IMU_BUS_SPI,
} imu_bus_t;

typedef enum {
    IMU_ACCEL_2G  = 0,
    IMU_ACCEL_4G  = 1,
    IMU_ACCEL_8G  = 2,
    IMU_ACCEL_16G = 3,
} imu_accel_range_t;

typedef enum {
    IMU_GYRO_250DPS  = 0,
    IMU_GYRO_500DPS  = 1,
    IMU_GYRO_1000DPS = 2,
    IMU_GYRO_2000DPS = 3,
} imu_gyro_range_t;

typedef struct {
    imu_model_t model;
    imu_bus_t bus;
    uint8_t addr;
    imu_accel_range_t accel_range;
    imu_gyro_range_t gyro_range;
    uint16_t sample_rate_hz;
    bool enable_fifo;
    bool enable_interrupt;
} imu_config_t;

typedef struct {
    float accel_x, accel_y, accel_z;  /* m/s² */
    float gyro_x, gyro_y, gyro_z;    /* rad/s */
    float temperature;                 /* °C */
    uint32_t timestamp_us;
} imu_sample_t;

typedef struct {
    float accel_bias[3];    /* m/s² */
    float accel_scale[3];   /* dimensionless */
    float gyro_bias[3];     /* rad/s */
    float misalign[3][3];   /* misalignment matrix */
} imu_calibration_t;

/* Platform-specific I2C read/write — implement per target */
extern int platform_i2c_read(uint8_t addr, uint8_t reg, uint8_t *buf, uint16_t len);
extern int platform_i2c_write(uint8_t addr, uint8_t reg, uint8_t *buf, uint16_t len);
extern void platform_delay_ms(uint32_t ms);

static imu_config_t _cfg;
static imu_calibration_t _cal;
static bool _initialized = false;

static int _write_reg(uint8_t reg, uint8_t val) {
    return platform_i2c_write(_cfg.addr, reg, &val, 1);
}

static int _read_reg(uint8_t reg, uint8_t *val) {
    return platform_i2c_read(_cfg.addr, reg, val, 1);
}

static int _read_regs(uint8_t reg, uint8_t *buf, uint16_t len) {
    return platform_i2c_read(_cfg.addr, reg, buf, len);
}

int imu_init(const imu_config_t *cfg) {
    memcpy(&_cfg, cfg, sizeof(imu_config_t));
    memset(&_cal, 0, sizeof(imu_calibration_t));
    _cal.accel_scale[0] = _cal.accel_scale[1] = _cal.accel_scale[2] = 1.0f;
    for (int i = 0; i < 3; i++) _cal.misalign[i][i] = 1.0f;

    uint8_t id = 0;
    switch (_cfg.model) {
    case IMU_MPU6050:
        _read_reg(0x75, &id);
        if (id != 0x68) return -1;
        _write_reg(0x6B, 0x80); platform_delay_ms(100);
        _write_reg(0x6B, 0x01);
        _write_reg(0x1C, (uint8_t)(_cfg.accel_range << 3));
        _write_reg(0x1B, (uint8_t)(_cfg.gyro_range << 3));
        break;
    case IMU_LSM6DS3:
        _read_reg(0x0F, &id);
        if (id != 0x69) return -1;
        _write_reg(0x12, 0x01); platform_delay_ms(50);
        _write_reg(0x12, 0x44);
        _write_reg(0x10, 0x60 | (_cfg.accel_range << 2));
        _write_reg(0x11, 0x60 | (_cfg.gyro_range << 2));
        break;
    case IMU_BMI270:
        _read_reg(0x00, &id);
        if (id != 0x24) return -1;
        _write_reg(0x7E, 0xB6); platform_delay_ms(200);
        /* BMI270 requires config upload — platform-specific */
        _write_reg(0x7D, 0x0E);
        _write_reg(0x40, 0xA8);
        _write_reg(0x41, _cfg.accel_range);
        _write_reg(0x42, 0xA9);
        _write_reg(0x43, _cfg.gyro_range);
        break;
    }
    _initialized = true;
    return 0;
}

int imu_read(imu_sample_t *sample) {
    if (!_initialized) return -1;
    uint8_t buf[14];
    int ret;

    switch (_cfg.model) {
    case IMU_MPU6050:
        ret = _read_regs(0x3B, buf, 14);
        if (ret != 0) return ret;
        {
            float a_scale = 9.81f / (16384.0f >> _cfg.accel_range);
            float g_scale = (3.14159f / 180.0f) / (131.0f / (1 << _cfg.gyro_range));
            int16_t raw_ax = (int16_t)((buf[0] << 8) | buf[1]);
            int16_t raw_ay = (int16_t)((buf[2] << 8) | buf[3]);
            int16_t raw_az = (int16_t)((buf[4] << 8) | buf[5]);
            int16_t raw_gx = (int16_t)((buf[8] << 8) | buf[9]);
            int16_t raw_gy = (int16_t)((buf[10] << 8) | buf[11]);
            int16_t raw_gz = (int16_t)((buf[12] << 8) | buf[13]);
            sample->accel_x = raw_ax * a_scale;
            sample->accel_y = raw_ay * a_scale;
            sample->accel_z = raw_az * a_scale;
            sample->gyro_x = raw_gx * g_scale;
            sample->gyro_y = raw_gy * g_scale;
            sample->gyro_z = raw_gz * g_scale;
            int16_t raw_t = (int16_t)((buf[6] << 8) | buf[7]);
            sample->temperature = raw_t / 340.0f + 36.53f;
        }
        break;
    default:
        return -2; /* TODO: LSM6DS3 / BMI270 read paths */
    }
    return 0;
}

void imu_set_calibration(const imu_calibration_t *cal) {
    memcpy(&_cal, cal, sizeof(imu_calibration_t));
}

void imu_apply_calibration(imu_sample_t *sample) {
    float ax = (sample->accel_x - _cal.accel_bias[0]) * _cal.accel_scale[0];
    float ay = (sample->accel_y - _cal.accel_bias[1]) * _cal.accel_scale[1];
    float az = (sample->accel_z - _cal.accel_bias[2]) * _cal.accel_scale[2];
    sample->accel_x = _cal.misalign[0][0]*ax + _cal.misalign[0][1]*ay + _cal.misalign[0][2]*az;
    sample->accel_y = _cal.misalign[1][0]*ax + _cal.misalign[1][1]*ay + _cal.misalign[1][2]*az;
    sample->accel_z = _cal.misalign[2][0]*ax + _cal.misalign[2][1]*ay + _cal.misalign[2][2]*az;
    sample->gyro_x -= _cal.gyro_bias[0];
    sample->gyro_y -= _cal.gyro_bias[1];
    sample->gyro_z -= _cal.gyro_bias[2];
}
