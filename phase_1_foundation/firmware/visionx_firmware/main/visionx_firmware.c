#include <stdio.h>

#include "esp_log.h"

#include "visionx_camera.h"

#define TAG "VISIONX_MAIN"

void app_main(void)
{
    ESP_LOGI(TAG, "========================================");
    ESP_LOGI(TAG, "VISIONX - Edge AI Perception & Intelligence System");
    ESP_LOGI(TAG, "Phase 1 - Camera Foundation Test");
    ESP_LOGI(TAG, "========================================");

    /* Initialize OV5640 */
    esp_err_t err = visionx_camera_init();

    if (err != ESP_OK) {
        ESP_LOGE(TAG, "OV5640 initialization failed");
        return;
    }

    ESP_LOGI(TAG, "OV5640 initialization successful");

    /* Capture one test frame */
    camera_fb_t *fb = visionx_camera_capture();

    if (fb == NULL) {
        ESP_LOGE(TAG, "Camera capture failed");
        return;
    }

    ESP_LOGI(
        TAG,
        "Frame captured: %ux%u, %u bytes",
        fb->width,
        fb->height,
        (unsigned int)fb->len
    );

    /* Return frame buffer */
    visionx_camera_return_frame(fb);

    ESP_LOGI(TAG, "Camera foundation test completed");
}