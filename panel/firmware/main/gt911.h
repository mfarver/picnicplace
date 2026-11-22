/* The GT911 touch controller: the first touch point, polled.
 *
 * This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
 */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "driver/i2c_master.h"
#include "esp_err.h"

#define GT911_ADDR 0x5D

typedef struct {
    i2c_master_dev_handle_t dev;
} gt911_t;

/* Attach to the controller, and check it is a GT911. */
esp_err_t gt911_init(gt911_t *touch, i2c_master_bus_handle_t bus);

/* Poll for a new report: false if none is ready (the last one still holds). Otherwise
 * true, with whether a finger is down and, if so, where. */
bool gt911_poll(gt911_t *touch, bool *down, uint16_t *x, uint16_t *y);
