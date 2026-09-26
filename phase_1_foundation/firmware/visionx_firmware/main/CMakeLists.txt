idf_component_register(
    SRCS
        "visionx_firmware.c"
        "visionx_camera.c"

    INCLUDE_DIRS
        "."

    REQUIRES
        esp32-camera
)