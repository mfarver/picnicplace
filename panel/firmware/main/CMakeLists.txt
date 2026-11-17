idf_component_register(
    SRCS "main.c" "board.c" "expander.c" "gt911.c" "icon.c" "link.c" "protocol.c" "ui.c"
    INCLUDE_DIRS "."
    REQUIRES esp_lcd esp_driver_i2c esp_driver_gpio esp_driver_uart esp_driver_usb_serial_jtag
             esp_timer esp_psram esp_app_format
)
