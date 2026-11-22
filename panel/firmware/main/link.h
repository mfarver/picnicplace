/* The serial link to the core: lines of text in and out.
 *
 * This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
 */
#pragma once

#include "esp_err.h"

typedef void (*link_line_cb_t)(const char *line);

/* Open the link (UART0 or native USB, as configured) and start reading: each line
 * received is passed to ``on_line``, from the link's own task. */
esp_err_t link_init(link_line_cb_t on_line);

/* Send one line; the newline is added. Safe from any task. */
void link_send(const char *line);
