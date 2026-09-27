idf_component_register(
    SRCS "main.c"
    INCLUDE_DIRS "."
    PRIV_REQUIRES
        esp32-camera
        esp_wifi
        esp_event
        esp_netif
        nvs_flash
        lwip
)