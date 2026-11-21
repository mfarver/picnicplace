/* The board's I/O expander: eight outputs (EXIO0-EXIO7) and the backlight's PWM.
 *
 * The expander is a small microcontroller with Waveshare's firmware, at I2C address 0x24.
 * Each command is written as two bytes, the command and its value. Commands, as
 * Waveshare's 7B examples use them:
 *   0x02  pin directions, one bit per EXIO pin, 1 for an output
 *   0x03  output levels, one bit per EXIO pin, 1 for high
 *   0x04  input levels (read one byte)
 *   0x05  the backlight PWM's duty, one byte: the higher, the dimmer (0 is full
 *         brightness, as found on the board)
 *   0x06  the ADC (read two bytes)
 * The expander takes the whole output byte at once, so the levels last written are kept
 * here.
 *
 * This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
 */
#include "expander.h"

#include "board.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG = "expander";

#define CMD_DIRECTION 0x02
#define CMD_OUTPUT 0x03
#define CMD_PWM 0x05
#define TIMEOUT_MS 20
#define LCD_RESET_MS 20
#define LCD_WAKE_MS 120

static i2c_master_dev_handle_t s_dev;
static uint8_t s_outputs;

static esp_err_t command(uint8_t cmd, uint8_t value)
{
    uint8_t buf[2] = {cmd, value};
    return i2c_master_transmit(s_dev, buf, sizeof buf, TIMEOUT_MS);
}

esp_err_t expander_set(int pin, bool high)
{
    uint8_t outputs = high ? (s_outputs | (1u << pin)) : (s_outputs & ~(1u << pin));
    esp_err_t err = command(CMD_OUTPUT, outputs);
    if (err == ESP_OK) {
        s_outputs = outputs;
    }
    return err;
}

esp_err_t expander_backlight(int level)
{
    level = level < 0 ? 0 : (level > 100 ? 100 : level);
    return command(CMD_PWM, (uint8_t)((100 - level) * 255 / 100));
}

esp_err_t expander_init(i2c_master_bus_handle_t bus)
{
    i2c_device_config_t config = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7,
        .device_address = BOARD_EXPANDER_ADDR,
        .scl_speed_hz = BOARD_I2C_HZ,
    };
    esp_err_t err = i2c_master_bus_add_device(bus, &config, &s_dev);
    if (err == ESP_OK) {
        err = command(CMD_DIRECTION, 0xFF);
    }
    if (err != ESP_OK) {
        return err;
    }
    /* Everything high (powered, out of reset, deselected), but native USB selected and
     * the screen held in reset for a moment. */
    s_outputs = 0xFF & ~(1u << EXIO_USB_SELECT) & ~(1u << EXIO_LCD_RESET);
    err = command(CMD_OUTPUT, s_outputs);
    if (err != ESP_OK) {
        return err;
    }
    vTaskDelay(pdMS_TO_TICKS(LCD_RESET_MS));
    err = expander_set(EXIO_LCD_RESET, true);
    vTaskDelay(pdMS_TO_TICKS(LCD_WAKE_MS));
    if (err == ESP_OK) {
        err = expander_backlight(100);
    }
    ESP_LOGI(TAG, "outputs 0x%02x", s_outputs);
    return err;
}
