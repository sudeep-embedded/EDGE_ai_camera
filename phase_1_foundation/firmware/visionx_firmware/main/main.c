#include <stdio.h>
#include <string.h>
#include <stdbool.h>
#include <stdint.h>
#include <errno.h>
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
#include "esp_rom_sys.h"
#include "esp_timer.h"
#include "nvs_flash.h"

#define TAG "OV5640_WIFI"

/* Wi-Fi */
#define WIFI_SSID       "2004"
#define WIFI_PASSWORD   "1234567890"

/* Laptop UDP receiver */
#define RECEIVER_IP     "10.127.246.188"
#define RECEIVER_PORT   5500

/* Camera quality: retain the proven setting */
#define JPEG_QUALITY    6

/* UDP protocol: packet size includes the 26-byte header */
#define UDP_PACKET_SIZE     1400
#define UDP_HEADER_SIZE     26
#define UDP_CHUNK_DATA      (UDP_PACKET_SIZE - UDP_HEADER_SIZE)
#define MAX_JPEG_SIZE       (256 * 1024)

/* Bounded handling of transient lwIP send-buffer pressure */
#define UDP_ENOMEM_RETRIES  2
#define UDP_RETRY_DELAY_US  1000
#define UDP_CHUNK_GAP_US    200

#define WIFI_CONNECTED_BIT BIT0

static EventGroupHandle_t wifi_event_group;
static int udp_socket_fd = -1;
static struct sockaddr_in receiver_addr;

/* ---------------------------------------------------------
   Wi-Fi event handler
   --------------------------------------------------------- */

static void wifi_event_handler(
    void *arg,
    esp_event_base_t event_base,
    int32_t event_id,
    void *event_data)
{
    (void)arg;

    if (event_base == WIFI_EVENT &&
        event_id == WIFI_EVENT_STA_START) {

        esp_wifi_connect();

    } else if (event_base == WIFI_EVENT &&
               event_id == WIFI_EVENT_STA_DISCONNECTED) {

        xEventGroupClearBits(
            wifi_event_group,
            WIFI_CONNECTED_BIT
        );

        ESP_LOGW(TAG, "Wi-Fi disconnected; reconnecting");
        esp_wifi_connect();

    } else if (event_base == IP_EVENT &&
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

/* ---------------------------------------------------------
   Wi-Fi initialization
   --------------------------------------------------------- */

static esp_err_t wifi_init(void)
{
    wifi_event_group = xEventGroupCreate();

    if (wifi_event_group == NULL) {
        ESP_LOGE(TAG, "Cannot create Wi-Fi event group");
        return ESP_ERR_NO_MEM;
    }

    esp_err_t err = nvs_flash_init();

    if (err == ESP_ERR_NVS_NO_FREE_PAGES ||
        err == ESP_ERR_NVS_NEW_VERSION_FOUND) {

        err = nvs_flash_erase();
        if (err != ESP_OK) {
            return err;
        }

        err = nvs_flash_init();
    }

    if (err != ESP_OK) {
        ESP_LOGE(TAG, "NVS initialization failed: %s",
                 esp_err_to_name(err));
        return err;
    }

    err = esp_netif_init();
    if (err != ESP_OK) {
        return err;
    }

    err = esp_event_loop_create_default();
    if (err != ESP_OK) {
        return err;
    }

    if (esp_netif_create_default_wifi_sta() == NULL) {
        ESP_LOGE(TAG, "Cannot create Wi-Fi station interface");
        return ESP_FAIL;
    }

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();

    err = esp_wifi_init(&cfg);
    if (err != ESP_OK) {
        return err;
    }

    err = esp_event_handler_register(
        WIFI_EVENT,
        ESP_EVENT_ANY_ID,
        wifi_event_handler,
        NULL
    );
    if (err != ESP_OK) {
        return err;
    }

    err = esp_event_handler_register(
        IP_EVENT,
        IP_EVENT_STA_GOT_IP,
        wifi_event_handler,
        NULL
    );
    if (err != ESP_OK) {
        return err;
    }

    wifi_config_t wifi_config = {0};

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

    wifi_config.sta.threshold.authmode = WIFI_AUTH_WPA2_PSK;
    wifi_config.sta.pmf_cfg.capable = true;
    wifi_config.sta.pmf_cfg.required = false;

    err = esp_wifi_set_mode(WIFI_MODE_STA);
    if (err != ESP_OK) {
        return err;
    }

    err = esp_wifi_set_config(WIFI_IF_STA, &wifi_config);
    if (err != ESP_OK) {
        return err;
    }

    /* Disable power saving for lower streaming latency. */
    err = esp_wifi_set_ps(WIFI_PS_NONE);
    if (err != ESP_OK) {
        return err;
    }

    err = esp_wifi_start();
    if (err != ESP_OK) {
        return err;
    }

    ESP_LOGI(TAG, "Connecting to Wi-Fi: %s", WIFI_SSID);

    EventBits_t bits = xEventGroupWaitBits(
        wifi_event_group,
        WIFI_CONNECTED_BIT,
        pdFALSE,
        pdTRUE,
        pdMS_TO_TICKS(20000)
    );

    if ((bits & WIFI_CONNECTED_BIT) == 0) {
        ESP_LOGE(TAG, "Wi-Fi connection timeout");
        return ESP_ERR_TIMEOUT;
    }

    ESP_LOGI(TAG, "Wi-Fi connected");
    return ESP_OK;
}

/* ---------------------------------------------------------
   UDP initialization
   --------------------------------------------------------- */

static esp_err_t udp_init(void)
{
    udp_socket_fd = socket(
        AF_INET,
        SOCK_DGRAM,
        IPPROTO_IP
    );

    if (udp_socket_fd < 0) {
        ESP_LOGE(TAG, "UDP socket creation failed: errno=%d", errno);
        return ESP_FAIL;
    }

    memset(&receiver_addr, 0, sizeof(receiver_addr));

    receiver_addr.sin_family = AF_INET;
    receiver_addr.sin_port = htons(RECEIVER_PORT);

    if (inet_pton(
            AF_INET,
            RECEIVER_IP,
            &receiver_addr.sin_addr
        ) != 1) {

        ESP_LOGE(TAG, "Invalid receiver IP: %s", RECEIVER_IP);
        return ESP_FAIL;
    }

    ESP_LOGI(
        TAG,
        "UDP receiver: %s:%d",
        RECEIVER_IP,
        RECEIVER_PORT
    );

    return ESP_OK;
}

/* ---------------------------------------------------------
   Camera initialization
   --------------------------------------------------------- */

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

        .pixel_format   = PIXFORMAT_JPEG,
        .frame_size     = FRAMESIZE_VGA,
        .jpeg_quality   = JPEG_QUALITY,

        .fb_count       = 2,
        .grab_mode      = CAMERA_GRAB_LATEST,
        .fb_location    = CAMERA_FB_IN_PSRAM
    };

    esp_err_t err = esp_camera_init(&config);

    if (err != ESP_OK) {
        ESP_LOGE(TAG, "Camera init failed: 0x%x", err);
        return err;
    }

    sensor_t *s = esp_camera_sensor_get();

    if (s == NULL) {
        ESP_LOGE(TAG, "OV5640 sensor not found");
        return ESP_FAIL;
    }

    ESP_LOGI(TAG, "OV5640 detected");

    /* Preserve the existing sensor settings. */
    s->set_exposure_ctrl(s, 1);
    s->set_gain_ctrl(s, 1);
    s->set_whitebal(s, 1);
    s->set_awb_gain(s, 1);
    s->set_ae_level(s, 2);
    s->set_agc_gain(s, 0);
    s->set_gainceiling(s, GAINCEILING_16X);

    s->set_brightness(s, 2);
    s->set_contrast(s, 0);
    s->set_saturation(s, 1);

    s->set_lenc(s, 1);
    s->set_bpc(s, 1);
    s->set_wpc(s, 1);
    s->set_raw_gma(s, 1);
    s->set_dcw(s, 1);
    s->set_special_effect(s, 0);
    s->set_hmirror(s, 0);
    s->set_vflip(s, 0);

    /* Allow auto exposure and white balance to stabilize. */
    vTaskDelay(pdMS_TO_TICKS(3000));

    ESP_LOGI(
        TAG,
        "Camera ready: VGA 640x480, JPEG quality %d",
        JPEG_QUALITY
    );

    return ESP_OK;
}

/* ---------------------------------------------------------
   Send one UDP packet
   --------------------------------------------------------- */

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
    uint8_t packet[UDP_PACKET_SIZE];

    /* Header magic */
    memcpy(packet, "OV56UDP1", 8);

    /* Sequence number, little-endian */
    packet[8]  = (uint8_t)(sequence);
    packet[9]  = (uint8_t)(sequence >> 8);
    packet[10] = (uint8_t)(sequence >> 16);
    packet[11] = (uint8_t)(sequence >> 24);

    /* Image dimensions */
    packet[12] = (uint8_t)(width);
    packet[13] = (uint8_t)(width >> 8);
    packet[14] = (uint8_t)(height);
    packet[15] = (uint8_t)(height >> 8);

    /* Chunk index and total chunks */
    packet[16] = (uint8_t)(chunk_index);
    packet[17] = (uint8_t)(chunk_index >> 8);
    packet[18] = (uint8_t)(total_chunks);
    packet[19] = (uint8_t)(total_chunks >> 8);

    /* Total JPEG size */
    packet[20] = (uint8_t)(jpeg_length);
    packet[21] = (uint8_t)(jpeg_length >> 8);
    packet[22] = (uint8_t)(jpeg_length >> 16);
    packet[23] = (uint8_t)(jpeg_length >> 24);

    /* This chunk's payload length */
    packet[24] = (uint8_t)(data_length);
    packet[25] = (uint8_t)(data_length >> 8);

    if ((size_t)data_length + UDP_HEADER_SIZE > sizeof(packet)) {
        return false;
    }

    memcpy(packet + UDP_HEADER_SIZE, data, data_length);

    const int packet_length = UDP_HEADER_SIZE + data_length;
    int attempts = 0;

    while (true) {
        int sent = sendto(
            udp_socket_fd,
            packet,
            packet_length,
            0,
            (struct sockaddr *)&receiver_addr,
            sizeof(receiver_addr)
        );

        if (sent == packet_length) {
            /*
             * A small inter-packet gap reduces burst pressure on lwIP.
             * Measure performance before changing this value.
             */
            esp_rom_delay_us(UDP_CHUNK_GAP_US);
            return true;
        }

        int send_errno = errno;

        /*
         * Retry only temporary memory-pressure errors.
         * The retry count is bounded so one frame cannot stall forever.
         */
        if (send_errno != ENOMEM ||
            attempts >= UDP_ENOMEM_RETRIES) {

            ESP_LOGW(
                TAG,
                "UDP send failed: errno=%d, packet=%d bytes",
                send_errno,
                packet_length
            );

            return false;
        }

        attempts++;
        esp_rom_delay_us(UDP_RETRY_DELAY_US);
    }
}

/* ---------------------------------------------------------
   Send one complete JPEG frame
   --------------------------------------------------------- */

static bool send_frame(
    camera_fb_t *fb,
    uint32_t sequence)
{
    if (fb == NULL ||
        fb->format != PIXFORMAT_JPEG ||
        fb->len < 4 ||
        fb->len > MAX_JPEG_SIZE) {

        return false;
    }

    const uint32_t total_chunks =
        ((uint32_t)fb->len + UDP_CHUNK_DATA - 1) /
        UDP_CHUNK_DATA;

    if (total_chunks == 0 || total_chunks > UINT16_MAX) {
        return false;
    }

    size_t offset = 0;

    for (uint32_t chunk = 0; chunk < total_chunks; chunk++) {
        size_t remaining = fb->len - offset;
        size_t chunk_size = remaining;

        if (chunk_size > UDP_CHUNK_DATA) {
            chunk_size = UDP_CHUNK_DATA;
        }

        if (!send_udp_chunk(
                sequence,
                fb->width,
                fb->height,
                (uint16_t)chunk,
                (uint16_t)total_chunks,
                (uint32_t)fb->len,
                fb->buf + offset,
                (uint16_t)chunk_size)) {

            /*
             * Do not send the remaining chunks of this frame.
             * The receiver will discard the incomplete frame.
             */
            return false;
        }

        offset += chunk_size;
    }

    return true;
}

/* ---------------------------------------------------------
   Main application
   --------------------------------------------------------- */

void app_main(void)
{
    ESP_LOGI(TAG, "Starting ESP32-S3 OV5640 wireless camera");

    if (camera_init() != ESP_OK) {
        ESP_LOGE(TAG, "Camera initialization failed");
        return;
    }

    if (wifi_init() != ESP_OK) {
        ESP_LOGE(TAG, "Wi-Fi initialization failed");
        return;
    }

    if (udp_init() != ESP_OK) {
        ESP_LOGE(TAG, "UDP initialization failed");
        return;
    }

    ESP_LOGI(TAG, "========================================");
    ESP_LOGI(TAG, "WIRELESS CAMERA READY");
    ESP_LOGI(TAG, "Receiver: %s:%d", RECEIVER_IP, RECEIVER_PORT);
    ESP_LOGI(TAG, "========================================");

    uint32_t sequence = 0;
    uint32_t frames_ok = 0;
    uint32_t frames_failed = 0;
    uint32_t camera_failures = 0;
    uint64_t bytes_sent_frames = 0;
    int64_t stats_start_us = esp_timer_get_time();

    while (true) {
        camera_fb_t *fb = esp_camera_fb_get();

        if (fb == NULL) {
            camera_failures++;
            vTaskDelay(pdMS_TO_TICKS(5));
            continue;
        }

        /*
         * Advance the sequence for every attempted JPEG frame,
         * including frames that fail partway through transmission.
         */
        const uint32_t this_sequence = sequence++;

        if (fb->format == PIXFORMAT_JPEG && fb->len > 100) {
            if (send_frame(fb, this_sequence)) {
                frames_ok++;
                bytes_sent_frames += fb->len;
            } else {
                frames_failed++;
            }
        } else {
            frames_failed++;
        }

        esp_camera_fb_return(fb);

        const int64_t now_us = esp_timer_get_time();

        if (now_us - stats_start_us >= 1000000) {
            const uint32_t avg_jpeg_kb =
                frames_ok
                    ? (uint32_t)(bytes_sent_frames / frames_ok / 1024)
                    : 0;

            ESP_LOGI(
                TAG,
                "1s stats | sent=%lu failed=%lu camera_fail=%lu avg_jpeg=%lu KB",
                (unsigned long)frames_ok,
                (unsigned long)frames_failed,
                (unsigned long)camera_failures,
                (unsigned long)avg_jpeg_kb
            );

            frames_ok = 0;
            frames_failed = 0;
            camera_failures = 0;
            bytes_sent_frames = 0;
            stats_start_us = now_us;
        }
    }
}