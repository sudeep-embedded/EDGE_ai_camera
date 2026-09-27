#include <stdio.h>
#include <string.h>
#include <stdbool.h>
#include <stdint.h>
#include <sys/socket.h>
#include <arpa/inet.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/event_groups.h"

#include "esp_camera.h"
#include "esp_log.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "nvs_flash.h"

#define TAG "OV5640_WIFI"

/* =========================================================
   WIFI SETTINGS
   ========================================================= */

#define WIFI_SSID       "2004"
#define WIFI_PASSWORD   "1234567890"

#define RECEIVER_IP     "10.59.216.188"
#define RECEIVER_PORT   5000

/* =========================================================
   CAMERA SETTINGS
   ========================================================= */

#define JPEG_QUALITY    6

/* =========================================================
   UDP SETTINGS
   ========================================================= */

#define UDP_PACKET_SIZE     1400
#define UDP_HEADER_SIZE     26
#define UDP_CHUNK_DATA      (UDP_PACKET_SIZE - UDP_HEADER_SIZE)
#define MAX_JPEG_SIZE       (256 * 1024)

/* =========================================================
   WIFI EVENT
   ========================================================= */

#define WIFI_CONNECTED_BIT  BIT0

static EventGroupHandle_t wifi_event_group;

/* =========================================================
   UDP SOCKET
   ========================================================= */

static int udp_socket_fd = -1;

static struct sockaddr_in receiver_addr;

/* =========================================================
   WIFI EVENT HANDLER
   ========================================================= */

static void wifi_event_handler(
    void *arg,
    esp_event_base_t event_base,
    int32_t event_id,
    void *event_data)
{
    if (event_base == WIFI_EVENT &&
        event_id == WIFI_EVENT_STA_START) {

        ESP_LOGI(TAG, "Wi-Fi started");

        esp_wifi_connect();
    }

    else if (event_base == WIFI_EVENT &&
             event_id == WIFI_EVENT_STA_DISCONNECTED) {

        ESP_LOGW(TAG, "Wi-Fi disconnected");

        xEventGroupClearBits(
            wifi_event_group,
            WIFI_CONNECTED_BIT
        );

        esp_wifi_connect();
    }

    else if (event_base == IP_EVENT &&
             event_id == IP_EVENT_STA_GOT_IP) {

        ip_event_got_ip_t *event =
            (ip_event_got_ip_t *)event_data;

        ESP_LOGI(
            TAG,
            "ESP32-S3 IP address: " IPSTR,
            IP2STR(&event->ip_info.ip)
        );

        xEventGroupSetBits(
            wifi_event_group,
            WIFI_CONNECTED_BIT
        );
    }
}

/* =========================================================
   WIFI INITIALIZATION
   ========================================================= */

static esp_err_t wifi_init(void)
{
    wifi_event_group = xEventGroupCreate();

    if (wifi_event_group == NULL) {

        ESP_LOGE(
            TAG,
            "Failed to create Wi-Fi event group"
        );

        return ESP_FAIL;
    }

    esp_err_t err = nvs_flash_init();

    if (err == ESP_ERR_NVS_NO_FREE_PAGES ||
        err == ESP_ERR_NVS_NEW_VERSION_FOUND) {

        ESP_ERROR_CHECK(
            nvs_flash_erase()
        );

        err = nvs_flash_init();
    }

    if (err != ESP_OK) {

        ESP_LOGE(
            TAG,
            "NVS init failed: 0x%x",
            err
        );

        return err;
    }

    ESP_ERROR_CHECK(
        esp_netif_init()
    );

    ESP_ERROR_CHECK(
        esp_event_loop_create_default()
    );

    esp_netif_create_default_wifi_sta();

    wifi_init_config_t cfg =
        WIFI_INIT_CONFIG_DEFAULT();

    ESP_ERROR_CHECK(
        esp_wifi_init(&cfg)
    );

    ESP_ERROR_CHECK(
        esp_event_handler_register(
            WIFI_EVENT,
            ESP_EVENT_ANY_ID,
            &wifi_event_handler,
            NULL
        )
    );

    ESP_ERROR_CHECK(
        esp_event_handler_register(
            IP_EVENT,
            IP_EVENT_STA_GOT_IP,
            &wifi_event_handler,
            NULL
        )
    );

    wifi_config_t wifi_config = {
        .sta = {
            .threshold.authmode = WIFI_AUTH_WPA2_PSK,

            .pmf_cfg = {
                .capable = true,
                .required = false
            }
        }
    };

    strncpy(
        (char *)wifi_config.sta.ssid,
        WIFI_SSID,
        sizeof(wifi_config.sta.ssid) - 1
    );

    strncpy(
        (char *)wifi_config.sta.password,
        WIFI_PASSWORD,
        sizeof(wifi_config.sta.password) - 1
    );

    ESP_ERROR_CHECK(
        esp_wifi_set_mode(WIFI_MODE_STA)
    );

    ESP_ERROR_CHECK(
        esp_wifi_set_config(
            WIFI_IF_STA,
            &wifi_config
        )
    );

    /*
     * Disable Wi-Fi power saving
     * for lower video latency.
     */
    ESP_ERROR_CHECK(
        esp_wifi_set_ps(WIFI_PS_NONE)
    );

    ESP_ERROR_CHECK(
        esp_wifi_start()
    );

    ESP_LOGI(
        TAG,
        "Connecting to Wi-Fi: %s",
        WIFI_SSID
    );

    EventBits_t bits =
        xEventGroupWaitBits(
            wifi_event_group,
            WIFI_CONNECTED_BIT,
            pdFALSE,
            pdTRUE,
            pdMS_TO_TICKS(20000)
        );

    if (!(bits & WIFI_CONNECTED_BIT)) {

        ESP_LOGE(
            TAG,
            "Wi-Fi connection timeout"
        );

        return ESP_ERR_TIMEOUT;
    }

    ESP_LOGI(
        TAG,
        "Wi-Fi connected"
    );

    return ESP_OK;
}

/* =========================================================
   UDP INITIALIZATION
   ========================================================= */

static esp_err_t udp_init(void)
{
    udp_socket_fd = socket(
        AF_INET,
        SOCK_DGRAM,
        IPPROTO_IP
    );

    if (udp_socket_fd < 0) {

        ESP_LOGE(
            TAG,
            "Unable to create UDP socket"
        );

        return ESP_FAIL;
    }

    memset(
        &receiver_addr,
        0,
        sizeof(receiver_addr)
    );

    receiver_addr.sin_family =
        AF_INET;

    receiver_addr.sin_port =
        htons(RECEIVER_PORT);

    receiver_addr.sin_addr.s_addr =
        inet_addr(RECEIVER_IP);

    ESP_LOGI(
        TAG,
        "UDP receiver: %s:%d",
        RECEIVER_IP,
        RECEIVER_PORT
    );

    return ESP_OK;
}

/* =========================================================
   CAMERA INITIALIZATION
   ========================================================= */

static esp_err_t camera_init(void)
{
    camera_config_t config = {

        .pin_pwdn       = 15,
        .pin_reset      = 16,
        .pin_xclk       = 9,

        .pin_sccb_sda   = 13,
        .pin_sccb_scl   = 14,

        .pin_d7         = 1,
        .pin_d6         = 2,
        .pin_d5         = 3,
        .pin_d4         = 4,
        .pin_d3         = 5,
        .pin_d2         = 6,
        .pin_d1         = 7,
        .pin_d0         = 8,

        .pin_vsync      = 11,
        .pin_href       = 12,
        .pin_pclk       = 10,

        .xclk_freq_hz   = 20000000,

        /*
         * PROVEN CAMERA BASELINE
         */

        .pixel_format   = PIXFORMAT_JPEG,
        .frame_size     = FRAMESIZE_VGA,
        .jpeg_quality   = JPEG_QUALITY,

        .fb_count       = 2,
        .grab_mode      = CAMERA_GRAB_LATEST,
        .fb_location    = CAMERA_FB_IN_PSRAM
    };

    esp_err_t err =
        esp_camera_init(&config);

    if (err != ESP_OK) {

        ESP_LOGE(
            TAG,
            "Camera init failed: 0x%x",
            err
        );

        return err;
    }

    sensor_t *s =
        esp_camera_sensor_get();

    if (s == NULL) {

        ESP_LOGE(
            TAG,
            "OV5640 sensor not found"
        );

        return ESP_FAIL;
    }

    ESP_LOGI(
        TAG,
        "OV5640 detected"
    );

    /* =====================================================
       AUTOMATIC EXPOSURE / GAIN / WHITE BALANCE
       ===================================================== */

    s->set_exposure_ctrl(s, 1);

    s->set_gain_ctrl(s, 1);

    s->set_whitebal(s, 1);

    s->set_awb_gain(s, 1);

    s->set_ae_level(s, 2);

    s->set_agc_gain(s, 0);

    s->set_gainceiling(
        s,
        GAINCEILING_16X
    );

    /* =====================================================
       IMAGE QUALITY
       ===================================================== */

    s->set_brightness(s, 2);

    s->set_contrast(s, 0);

    s->set_saturation(s, 1);

    /* =====================================================
       SENSOR CORRECTIONS
       ===================================================== */

    s->set_lenc(s, 1);

    s->set_bpc(s, 1);

    s->set_wpc(s, 1);

    s->set_raw_gma(s, 1);

    s->set_dcw(s, 1);

    s->set_special_effect(s, 0);

    s->set_hmirror(s, 0);

    s->set_vflip(s, 0);

    /*
     * Allow automatic exposure,
     * gain and white balance to stabilize.
     */
    vTaskDelay(
        pdMS_TO_TICKS(3000)
    );

    ESP_LOGI(
        TAG,
        "========================================"
    );

    ESP_LOGI(
        TAG,
        "OV5640 WIRELESS CAMERA"
    );

    ESP_LOGI(
        TAG,
        "Resolution    : 640x480"
    );

    ESP_LOGI(
        TAG,
        "Native JPEG   : ON"
    );

    ESP_LOGI(
        TAG,
        "JPEG quality  : %d",
        JPEG_QUALITY
    );

    ESP_LOGI(
        TAG,
        "Auto exposure : ON"
    );

    ESP_LOGI(
        TAG,
        "Auto gain     : ON"
    );

    ESP_LOGI(
        TAG,
        "Auto WB       : ON"
    );

    ESP_LOGI(
        TAG,
        "Brightness    : +2"
    );

    ESP_LOGI(
        TAG,
        "Contrast      : 0"
    );

    ESP_LOGI(
        TAG,
        "Saturation    : +1"
    );

    ESP_LOGI(
        TAG,
        "Gain ceiling  : 16X"
    );

    ESP_LOGI(
        TAG,
        "========================================"
    );

    return ESP_OK;
}

/* =========================================================
   SEND ONE UDP CHUNK
   ========================================================= */

static bool send_udp_chunk(
    uint32_t sequence,
    uint16_t width,
    uint16_t height,
    uint16_t chunk_index,
    uint16_t total_chunks,
    uint32_t jpeg_length,
    const uint8_t *data,
    uint16_t data_length)
{
    /*
     * UDP HEADER = 26 BYTES
     *
     * 0-7     MAGIC
     * 8-11    sequence
     * 12-13   width
     * 14-15   height
     * 16-17   chunk index
     * 18-19   total chunks
     * 20-23   JPEG length
     * 24-25   chunk length
     */

    uint8_t packet[UDP_PACKET_SIZE];

    memcpy(
        packet,
        "OV56UDP1",
        8
    );

    /* Sequence */
    packet[8] =
        (sequence >> 0) & 0xFF;

    packet[9] =
        (sequence >> 8) & 0xFF;

    packet[10] =
        (sequence >> 16) & 0xFF;

    packet[11] =
        (sequence >> 24) & 0xFF;

    /* Width */
    packet[12] =
        width & 0xFF;

    packet[13] =
        (width >> 8) & 0xFF;

    /* Height */
    packet[14] =
        height & 0xFF;

    packet[15] =
        (height >> 8) & 0xFF;

    /* Chunk index */
    packet[16] =
        chunk_index & 0xFF;

    packet[17] =
        (chunk_index >> 8) & 0xFF;

    /* Total chunks */
    packet[18] =
        total_chunks & 0xFF;

    packet[19] =
        (total_chunks >> 8) & 0xFF;

    /* Complete JPEG length */
    packet[20] =
        (jpeg_length >> 0) & 0xFF;

    packet[21] =
        (jpeg_length >> 8) & 0xFF;

    packet[22] =
        (jpeg_length >> 16) & 0xFF;

    packet[23] =
        (jpeg_length >> 24) & 0xFF;

    /* Current chunk length */
    packet[24] =
        data_length & 0xFF;

    packet[25] =
        (data_length >> 8) & 0xFF;

    /* JPEG payload */
    memcpy(
        packet + UDP_HEADER_SIZE,
        data,
        data_length
    );

    int packet_length =
        UDP_HEADER_SIZE + data_length;

    int sent = sendto(
        udp_socket_fd,
        packet,
        packet_length,
        0,
        (struct sockaddr *)&receiver_addr,
        sizeof(receiver_addr)
    );

    if (sent != packet_length) {
        return false;
    }

    return true;
}

/* =========================================================
   SEND COMPLETE JPEG FRAME
   ========================================================= */

static bool send_frame(
    camera_fb_t *fb,
    uint32_t sequence)
{
    if (fb == NULL) {
        return false;
    }

    if (fb->len == 0 ||
        fb->len > MAX_JPEG_SIZE) {

        ESP_LOGW(
            TAG,
            "Invalid JPEG size: %lu",
            (unsigned long)fb->len
        );

        return false;
    }

    /*
     * Use uint32_t here so the range check
     * is valid with ESP-IDF -Werror.
     */
    uint32_t total_chunks =
        (fb->len + UDP_CHUNK_DATA - 1) /
        UDP_CHUNK_DATA;

    if (total_chunks == 0 ||
        total_chunks > 65535) {

        return false;
    }

    size_t offset = 0;

    for (uint32_t chunk = 0;
         chunk < total_chunks;
         chunk++) {

        size_t remaining =
            fb->len - offset;

        size_t chunk_size =
            remaining;

        if (chunk_size >
            UDP_CHUNK_DATA) {

            chunk_size =
                UDP_CHUNK_DATA;
        }

        bool ok =
            send_udp_chunk(
                sequence,
                fb->width,
                fb->height,
                (uint16_t)chunk,
                (uint16_t)total_chunks,
                fb->len,
                fb->buf + offset,
                (uint16_t)chunk_size
            );

        if (!ok) {
            return false;
        }

        offset += chunk_size;
    }

    return true;
}

/* =========================================================
   MAIN
   ========================================================= */

void app_main(void)
{
    ESP_LOGI(
        TAG,
        "Starting ESP32-S3 OV5640 wireless camera"
    );

    /* =====================================================
       CAMERA FIRST
       ===================================================== */

    if (camera_init() != ESP_OK) {

        ESP_LOGE(
            TAG,
            "Camera initialization failed"
        );

        while (1) {

            vTaskDelay(
                pdMS_TO_TICKS(1000)
            );
        }
    }

    /* =====================================================
       WIFI
       ===================================================== */

    if (wifi_init() != ESP_OK) {

        ESP_LOGE(
            TAG,
            "Wi-Fi initialization failed"
        );

        while (1) {

            vTaskDelay(
                pdMS_TO_TICKS(1000)
            );
        }
    }

    /* =====================================================
       UDP
       ===================================================== */

    if (udp_init() != ESP_OK) {

        ESP_LOGE(
            TAG,
            "UDP initialization failed"
        );

        while (1) {

            vTaskDelay(
                pdMS_TO_TICKS(1000)
            );
        }
    }

    ESP_LOGI(
        TAG,
        "========================================"
    );

    ESP_LOGI(
        TAG,
        "WIRELESS CAMERA READY"
    );

    ESP_LOGI(
        TAG,
        "Receiver: %s:%d",
        RECEIVER_IP,
        RECEIVER_PORT
    );

    ESP_LOGI(
        TAG,
        "========================================"
    );

    uint32_t sequence = 0;

    /* =====================================================
       LIVE CAMERA LOOP
       ===================================================== */

    while (1) {

        camera_fb_t *fb =
            esp_camera_fb_get();

        if (fb == NULL) {

            ESP_LOGW(
                TAG,
                "Camera frame capture failed"
            );

            vTaskDelay(
                pdMS_TO_TICKS(10)
            );

            continue;
        }

        if (fb->format == PIXFORMAT_JPEG &&
            fb->len > 100) {

            if (send_frame(
                    fb,
                    sequence)) {

                sequence++;
            }
        }

        esp_camera_fb_return(fb);

        /*
         * Low-latency streaming.
         */
        vTaskDelay(
            pdMS_TO_TICKS(1)
        );
    }
}