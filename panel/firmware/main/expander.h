/* The board's I/O expander: eight outputs (EXIO0-EXIO7) and the backlight's PWM.
 *
 * This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
 */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "driver/i2c_master.h"
#include "esp_err.h"

/* Attach to the expander, make every pin an output, and set the panel's start-up state:
 * the screen powered and out of reset, the backlight on, the touch controller out of
 * reset, the SD card deselected, and native USB selected (not CAN). Pulses the screen's
 * reset. */
esp_err_t expander_init(i2c_master_bus_handle_t bus);

/* Drive one EXIO pin high or low. */
esp_err_t expander_set(int pin, bool high);

/* Set the backlight's brightness, 0 (dark) to 100 (full). */
esp_err_t expander_backlight(int level);
