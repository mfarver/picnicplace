/* The Waveshare ESP32-S3-Touch-LCD-7B: its pins, its screen, and its I2C bus.
 *
 * This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
 */
#include "board.h"

#include "driver/gpio.h"
#include "esp_lcd_panel_rgb.h"
#include "esp_log.h"

static const char *TAG = "board";

/* The RGB bus, 16 bits (RGB565): blue on D0-D4, green on D5-D10, red on D11-D15. The
 * board wires the top bits of each color (B3-B7, G2-G7, R3-R7). */
static const int DATA_PINS[16] = {
    14, 38, 18, 17, 10,     /* B3-B7 */
    39, 0,  45, 48, 47, 21, /* G2-G7 */
    1,  2,  42, 41, 40,     /* R3-R7 */
};
#define LCD_VSYNC 3
#define LCD_HSYNC 46
#define LCD_DE 5
#define LCD_PCLK 7

/* The 7B's panel timing, as Waveshare's examples give it: about 33 frames a second. */
#define LCD_PCLK_HZ (30 * 1000 * 1000)
#define LCD_HSYNC_PULSE 162
#define LCD_HSYNC_BACK 152
#define LCD_HSYNC_FRONT 48
#define LCD_VSYNC_PULSE 45
#define LCD_VSYNC_BACK 13
#define LCD_VSYNC_FRONT 3

/* Lines the DMA reads from internal RAM at a time, so PSRAM's slower reads never starve
 * the screen. */
#define LCD_BOUNCE_LINES 10

esp_err_t board_i2c_init(i2c_master_bus_handle_t *bus)
{
    i2c_master_bus_config_t config = {
        .i2c_port = I2C_NUM_0,
        .sda_io_num = BOARD_I2C_SDA,
        .scl_io_num = BOARD_I2C_SCL,
        .clk_source = I2C_CLK_SRC_DEFAULT,
        .glitch_ignore_cnt = 7,
        .flags.enable_internal_pullup = true,
    };
    esp_err_t err = i2c_new_master_bus(&config, bus);
    if (err != ESP_OK) {
        return err;
    }
    for (uint16_t addr = 0x08; addr < 0x78; addr++) {
        if (i2c_master_probe(*bus, addr, 20) == ESP_OK) {
            ESP_LOGI(TAG, "I2C device at 0x%02x", addr);
        }
    }
    return ESP_OK;
}

esp_err_t board_lcd_init(esp_lcd_panel_handle_t *panel)
{
    esp_lcd_rgb_panel_config_t config = {
        .clk_src = LCD_CLK_SRC_DEFAULT,
        .timings = {
            .pclk_hz = LCD_PCLK_HZ,
            .h_res = BOARD_WIDTH,
            .v_res = BOARD_HEIGHT,
            .hsync_pulse_width = LCD_HSYNC_PULSE,
            .hsync_back_porch = LCD_HSYNC_BACK,
            .hsync_front_porch = LCD_HSYNC_FRONT,
            .vsync_pulse_width = LCD_VSYNC_PULSE,
            .vsync_back_porch = LCD_VSYNC_BACK,
            .vsync_front_porch = LCD_VSYNC_FRONT,
            .flags.pclk_active_neg = true,
        },
        .data_width = 16,
        .in_color_format = LCD_COLOR_FMT_RGB565,
        .out_color_format = LCD_COLOR_FMT_RGB565,
        .num_fbs = 2, /* double-buffered: see display.c */
        .bounce_buffer_size_px = BOARD_WIDTH * LCD_BOUNCE_LINES,
        .hsync_gpio_num = LCD_HSYNC,
        .vsync_gpio_num = LCD_VSYNC,
        .de_gpio_num = LCD_DE,
        .pclk_gpio_num = LCD_PCLK,
        .disp_gpio_num = -1,
        .flags.fb_in_psram = true,
    };
    for (int i = 0; i < 16; i++) {
        config.data_gpio_nums[i] = DATA_PINS[i];
    }
    esp_err_t err = esp_lcd_new_rgb_panel(&config, panel);
    if (err != ESP_OK) {
        return err;
    }
    err = esp_lcd_panel_reset(*panel);
    if (err == ESP_OK) {
        err = esp_lcd_panel_init(*panel);
    }
    return err;
}

void board_estop_init(void)
{
    gpio_config_t config = {
        .pin_bit_mask = 1ULL << BOARD_ESTOP_GPIO,
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE,
    };
    gpio_config(&config);
}

bool board_estop_pressed(void)
{
    return gpio_get_level(BOARD_ESTOP_GPIO) != 0;
}
