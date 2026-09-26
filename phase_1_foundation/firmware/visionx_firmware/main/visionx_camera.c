#include "visionx_camera.h"

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "esp_log.h"

#define TAG "VISIONX_CAMERA"

/* =========================================================
   OV5640 GPIO CONFIGURATION
   ========================================================= */

#define CAM_PIN_PWDN    15
#define CAM_PIN_RESET   16
#define CAM_PIN_XCLK     9

#define CAM_PIN_SIOD    13
#define CAM_PIN_SIOC    14

#define CAM_PIN_D7       1
#define CAM_PIN_D6       2
#define CAM_PIN_D5       3
#define CAM_PIN_D4       4
#define CAM_PIN_D3       5
#define CAM_PIN_D2       6
#define CAM_PIN_D1       7
#define CAM_PIN_D0       8

#define CAM_PIN_VSYNC   11
#define CAM_PIN_HREF    12
#define CAM_PIN_PCLK    10

/* =========================================================
   CAMERA SETTINGS
   ========================================================= */

#define CAMERA_XCLK_FREQ       20000000
#define CAMERA_JPEG_QUALITY    6

/* =========================================================
   CAMERA INITIALIZATION
   ========================================================= */

esp_err_t visionx_camera_init(void)
{
    camera_config_t config = {
        .pin_pwdn       = CAM_PIN_PWDN,
        .pin_reset      = CAM_PIN_RESET,
        .pin_xclk       = CAM_PIN_XCLK,

        .pin_sccb_sda   = CAM_PIN_SIOD,
        .pin_sccb_scl   = CAM_PIN_SIOC,

        .pin_d7         = CAM_PIN_D7,
        .pin_d6         = CAM_PIN_D6,
        .pin_d5         = CAM_PIN_D5,
        .pin_d4         = CAM_PIN_D4,
        .pin_d3         = CAM_PIN_D3,
        .pin_d2         = CAM_PIN_D2,
        .pin_d1         = CAM_PIN_D1,
        .pin_d0         = CAM_PIN_D0,

        .pin_vsync      = CAM_PIN_VSYNC,
        .pin_href       = CAM_PIN_HREF,
        .pin_pclk       = CAM_PIN_PCLK,

        .xclk_freq_hz   = CAMERA_XCLK_FREQ,

        .ledc_timer     = LEDC_TIMER_0,
        .ledc_channel   = LEDC_CHANNEL_0,

        .pixel_format   = PIXFORMAT_JPEG,
        .frame_size     = FRAMESIZE_VGA,
        .jpeg_quality   = CAMERA_JPEG_QUALITY,

        .fb_count       = 2,
        .grab_mode      = CAMERA_GRAB_LATEST,
        .fb_location    = CAMERA_FB_IN_PSRAM
    };

    ESP_LOGI(TAG, "Initializing OV5640 camera...");

    esp_err_t err = esp_camera_init(&config);

    if (err != ESP_OK) {
        ESP_LOGE(
            TAG,
            "Camera initialization failed: 0x%x",
            err
        );

        return err;
    }

    sensor_t *sensor = esp_camera_sensor_get();

    if (sensor == NULL) {
        ESP_LOGE(TAG, "OV5640 sensor not found");
        return ESP_FAIL;
    }

    ESP_LOGI(TAG, "OV5640 sensor detected");

    /* =====================================================
       AUTOMATIC EXPOSURE / GAIN / WHITE BALANCE
       ===================================================== */

    sensor->set_exposure_ctrl(sensor, 1);
    sensor->set_gain_ctrl(sensor, 1);
    sensor->set_whitebal(sensor, 1);
    sensor->set_awb_gain(sensor, 1);

    sensor->set_ae_level(sensor, 2);
    sensor->set_agc_gain(sensor, 0);

    sensor->set_gainceiling(
        sensor,
        GAINCEILING_16X
    );

    /* =====================================================
       IMAGE QUALITY
       ===================================================== */

    sensor->set_brightness(sensor, 2);
    sensor->set_contrast(sensor, 0);
    sensor->set_saturation(sensor, 1);

    /* =====================================================
       SENSOR CORRECTIONS
       ===================================================== */

    sensor->set_lenc(sensor, 1);
    sensor->set_bpc(sensor, 1);
    sensor->set_wpc(sensor, 1);
    sensor->set_raw_gma(sensor, 1);
    sensor->set_dcw(sensor, 1);

    sensor->set_special_effect(sensor, 0);

    sensor->set_hmirror(sensor, 0);
    sensor->set_vflip(sensor, 0);

    /*
     * Allow automatic exposure, gain and
     * white balance to stabilize.
     */
    vTaskDelay(pdMS_TO_TICKS(3000));

    ESP_LOGI(TAG, "----------------------------------------");
    ESP_LOGI(TAG, "VISIONX OV5640 CAMERA");
    ESP_LOGI(TAG, "Resolution    : 640x480");
    ESP_LOGI(TAG, "Native JPEG   : ON");
    ESP_LOGI(TAG, "JPEG quality  : %d", CAMERA_JPEG_QUALITY);
    ESP_LOGI(TAG, "Auto exposure : ON");
    ESP_LOGI(TAG, "Auto gain     : ON");
    ESP_LOGI(TAG, "Auto WB       : ON");
    ESP_LOGI(TAG, "Brightness    : +2");
    ESP_LOGI(TAG, "Contrast      : 0");
    ESP_LOGI(TAG, "Saturation    : +1");
    ESP_LOGI(TAG, "Gain ceiling  : 16X");
    ESP_LOGI(TAG, "----------------------------------------");

    return ESP_OK;
}

/* =========================================================
   CAPTURE FRAME
   ========================================================= */

camera_fb_t *visionx_camera_capture(void)
{
    camera_fb_t *fb = esp_camera_fb_get();

    if (fb == NULL) {
        ESP_LOGW(
            TAG,
            "Camera frame capture failed"
        );

        return NULL;
    }

    return fb;
}

/* =========================================================
   RETURN FRAME
   ========================================================= */

void visionx_camera_return_frame(camera_fb_t *fb)
{
    if (fb != NULL) {
        esp_camera_fb_return(fb);
    }
}