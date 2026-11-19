/* LVGL on the panel's screen: turned to portrait, and double-buffered.
 *
 * This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
 */
#pragma once

#include "esp_lcd_panel_ops.h"
#include "lvgl.h"

/* Create LVGL's display, ``width`` x ``height`` as drawn, on a panel started with two
 * frame buffers. */
lv_display_t *display_start(esp_lcd_panel_handle_t panel, int width, int height);
