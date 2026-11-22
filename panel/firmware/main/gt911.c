/* The GT911 touch controller: the first touch point, polled.
 *
 * Registers, from Goodix's GT911 datasheet: 16-bit addresses, high byte first.
 *   0x8140  product ID, four ASCII bytes ("911\0")
 *   0x814E  status: bit 7 set when new points are ready, bits 0-3 the number of points;
 *           written back as 0 once read
 *   0x814F  the first point: track ID, X (2 bytes, low first), Y, size, reserved
 *
 * This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
 */
#include "gt911.h"

#include <string.h>

#include "board.h"
#include "esp_log.h"

static const char *TAG = "gt911";

#define REG_PRODUCT_ID 0x8140
#define REG_STATUS 0x814E
#define REG_POINT 0x814F
#define TIMEOUT_MS 20

static esp_err_t read_reg(gt911_t *touch, uint16_t reg, uint8_t *out, size_t len)
{
    uint8_t addr[2] = {reg >> 8, reg & 0xFF};
    return i2c_master_transmit_receive(touch->dev, addr, 2, out, len, TIMEOUT_MS);
}

static esp_err_t write_reg(gt911_t *touch, uint16_t reg, uint8_t value)
{
    uint8_t buf[3] = {reg >> 8, reg & 0xFF, value};
    return i2c_master_transmit(touch->dev, buf, sizeof buf, TIMEOUT_MS);
}

esp_err_t gt911_init(gt911_t *touch, i2c_master_bus_handle_t bus)
{
    i2c_device_config_t config = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7,
        .device_address = GT911_ADDR,
        .scl_speed_hz = BOARD_I2C_HZ,
    };
    esp_err_t err = i2c_master_bus_add_device(bus, &config, &touch->dev);
    if (err != ESP_OK) {
        return err;
    }
    char id[5] = {0};
    err = read_reg(touch, REG_PRODUCT_ID, (uint8_t *)id, 4);
    if (err != ESP_OK) {
        return err;
    }
    ESP_LOGI(TAG, "product ID \"%s\"", id);
    return strcmp(id, "911") == 0 ? ESP_OK : ESP_ERR_NOT_FOUND;
}

bool gt911_poll(gt911_t *touch, bool *down, uint16_t *x, uint16_t *y)
{
    uint8_t status = 0;
    if (read_reg(touch, REG_STATUS, &status, 1) != ESP_OK || !(status & 0x80)) {
        return false;
    }
    int points = status & 0x0F;
    uint8_t point[8] = {0};
    *down = points > 0 && read_reg(touch, REG_POINT, point, sizeof point) == ESP_OK;
    write_reg(touch, REG_STATUS, 0);
    if (*down) {
        *x = point[1] | (point[2] << 8);
        *y = point[3] | (point[4] << 8);
    }
    return true;
}
