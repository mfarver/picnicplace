/* A seven-segment readout, drawn as segments rather than from a font: lit segments in
 * the readout's color, unlit ones faintly, like an LED display.
 *
 * This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
 */
#pragma once

#include "lvgl.h"

/* A readout ``digit_height`` pixels tall, in ``color``, right-aligned in its object.
 * Shows digits, '-', '.', and spaces. */
lv_obj_t *seg7_create(lv_obj_t *parent, int digit_height, lv_color_t color);

/* Show a text (at most 15 characters). */
void seg7_set_text(lv_obj_t *readout, const char *text);
