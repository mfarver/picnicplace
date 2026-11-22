/* The serial link to the core: lines of text in and out.
 *
 * Over UART0 (the board's USB-to-UART port) the link shares the line with the log, so
 * the core ignores lines that are not JSON. Over native USB it has the line to itself.
 *
 * This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
 */
#include "link.h"

#include <stdbool.h>
#include <stdint.h>
#include <string.h>

#include "driver/uart.h"
#include "driver/usb_serial_jtag.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "sdkconfig.h"

#define LINE_LIMIT 1536
#define BUFFER 2048
#define WRITE_PIECE 512

static link_line_cb_t s_on_line;
static SemaphoreHandle_t s_send_lock;

static int link_read(uint8_t *buf, size_t len)
{
#if CONFIG_PANEL_LINK_USB
    return usb_serial_jtag_read_bytes(buf, len, pdMS_TO_TICKS(100));
#else
    return uart_read_bytes(UART_NUM_0, buf, len, pdMS_TO_TICKS(100));
#endif
}

static void link_write(const char *data, size_t len)
{
#if CONFIG_PANEL_LINK_USB
    /* A long line (a screenshot's row) is more than the driver's buffer takes in one write,
     * which it refuses whole: send it in pieces. */
    while (len > 0) {
        size_t piece = len < WRITE_PIECE ? len : WRITE_PIECE;
        int sent = usb_serial_jtag_write_bytes(data, piece, pdMS_TO_TICKS(100));
        if (sent <= 0) {
            return; /* no host reading: drop the rest */
        }
        data += sent;
        len -= (size_t)sent;
    }
#else
    uart_write_bytes(UART_NUM_0, data, len);
#endif
}

static void reader(void *arg)
{
    static char line[LINE_LIMIT];
    size_t used = 0;
    bool overflow = false;
    uint8_t chunk[128];
    for (;;) {
        int n = link_read(chunk, sizeof chunk);
        for (int i = 0; i < n; i++) {
            char c = (char)chunk[i];
            if (c == '\n' || c == '\r') {
                if (used > 0 && !overflow) {
                    line[used] = '\0';
                    s_on_line(line);
                }
                used = 0;
                overflow = false;
            } else if (used < LINE_LIMIT - 1) {
                line[used++] = c;
            } else {
                overflow = true; /* too long for the panel: dropped whole */
            }
        }
    }
}

esp_err_t link_init(link_line_cb_t on_line)
{
    s_on_line = on_line;
    s_send_lock = xSemaphoreCreateMutex();
    esp_err_t err;
#if CONFIG_PANEL_LINK_USB
    usb_serial_jtag_driver_config_t config = {
        .rx_buffer_size = BUFFER,
        .tx_buffer_size = BUFFER,
    };
    err = usb_serial_jtag_driver_install(&config);
#else
    err = uart_driver_install(UART_NUM_0, BUFFER, BUFFER, 0, NULL, 0);
#endif
    if (err != ESP_OK) {
        return err;
    }
    return xTaskCreate(reader, "link", 6144, NULL, 5, NULL) == pdPASS ? ESP_OK : ESP_ERR_NO_MEM;
}

void link_send(const char *line)
{
    xSemaphoreTake(s_send_lock, portMAX_DELAY);
    link_write(line, strlen(line));
    link_write("\n", 1);
    xSemaphoreGive(s_send_lock);
}
