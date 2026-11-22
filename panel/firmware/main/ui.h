/* The panel's screen: the status bar, the position, the core's messages, and the keys.
 *
 * This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
 */
#pragma once

#include <stdint.h>

#include "protocol.h"

typedef void (*ui_press_cb_t)(const char *key);

/* Build the screen on LVGL's default display. ``on_press`` is called with a key's name
 * when the operator presses it. */
void ui_create(ui_press_cb_t on_press);

/* Show what the core sent. */
void ui_apply(const panel_event_t *event, uint32_t now_ms);

/* Called often: shows "No connection to the core" once nothing has come for a while. */
void ui_tick(uint32_t now_ms);
