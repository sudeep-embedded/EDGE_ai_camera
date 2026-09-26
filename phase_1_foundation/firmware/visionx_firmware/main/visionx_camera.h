#ifndef VISIONX_CAMERA_H
#define VISIONX_CAMERA_H

#include "esp_err.h"
#include "esp_camera.h"

/*
 * VISIONX OV5640 Camera Interface
 *
 * ESP32-S3 + OV5640
 *
 * GPIO mapping:
 * D9  -> GPIO1
 * D8  -> GPIO2
 * D7  -> GPIO3
 * D6  -> GPIO4
 * D5  -> GPIO5
 * D4  -> GPIO6
 * D3  -> GPIO7
 * D2  -> GPIO8
 *
 * XCLK -> GPIO9
 * PCLK -> GPIO10
 * VSYNC -> GPIO11
 * HREF -> GPIO12
 * SIOD -> GPIO13
 * SIOC -> GPIO14
 * PWDN -> GPIO15
 * RST  -> GPIO16
 */

/**
 * Initialize OV5640 camera.
 *
 * Configures:
 * - VGA 640x480
 * - Native JPEG
 * - JPEG quality 6
 * - 20 MHz XCLK
 * - 2 frame buffers
 * - PSRAM frame buffers
 * - CAMERA_GRAB_LATEST
 * - Auto exposure
 * - Auto gain
 * - Auto white balance
 */
esp_err_t visionx_camera_init(void);

/**
 * Capture one JPEG frame.
 *
 * The returned frame buffer belongs to the camera driver.
 * It must be returned using visionx_camera_return_frame().
 */
camera_fb_t *visionx_camera_capture(void);

/**
 * Return a previously captured frame buffer
 * back to the camera driver.
 */
void visionx_camera_return_frame(camera_fb_t *fb);

#endif /* VISIONX_CAMERA_H */