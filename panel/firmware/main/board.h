/* The Waveshare ESP32-S3-Touch-LCD-7B: its pins, its screen, and its I2C bus.
 *
 * Pin numbers come from Waveshare's wiki pinout table for the 7B; the screen's timing
 * and the I/O expander's pins from the facts in Waveshare's 7B examples.
 *
 * This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
 */
#pragma once

#include "driver/i2c_master.h"
#include "esp_err.h"
#include "esp_lcd_panel_ops.h"

#define BOARD_WIDTH 1024
#define BOARD_HEIGHT 600

/* The I2C bus: the touch controller (GT911, 0x5D) and the I/O expander (0x24). */
#define BOARD_I2C_SDA 8
#define BOARD_I2C_SCL 9
#define BOARD_I2C_HZ 400000
#define BOARD_TOUCH_INT 4
#define BOARD_EXPANDER_ADDR 0x24

/* The E-stop's sensing pole, on the GPIO header: normally closed, pulled up, so a pressed
 * switch or a broken wire both read high. */
#define BOARD_ESTOP_GPIO 6

/* The I/O expander's pins (EXIO0 to EXIO7); see expander.c. */
#define EXIO_TOUCH_RESET 1
#define EXIO_BACKLIGHT 2
#define EXIO_LCD_RESET 3
#define EXIO_SD_CS 4
#define EXIO_USB_SELECT 5 /* low: native USB on GPIO19/20; high: CAN */
#define EXIO_LCD_POWER 6

/* Bring up the I2C bus, and report which devices answer on it. */
esp_err_t board_i2c_init(i2c_master_bus_handle_t *bus);

/* Start the RGB screen, with two frame buffers in PSRAM. */
esp_err_t board_lcd_init(esp_lcd_panel_handle_t *panel);

/* Read the E-stop's sensing pole: true when pressed (or its wire is broken). */
bool board_estop_pressed(void);
void board_estop_init(void);
