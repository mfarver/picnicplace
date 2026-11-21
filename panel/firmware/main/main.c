/* The PicnicPlace control panel: a touchscreen that shows the machine's state and sends
 * the operator's keys to the core, over a serial link. It keeps no state of its own and
 * never talks to the machine's controller: every key is checked by the core, as a click
 * in the Studio is. See docs/hardware/control-panel.md.
 *
 * This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
 */
#include <stdio.h>

#include "board.h"
#include "display.h"
#include "esp_app_desc.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "expander.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"
#include "gt911.h"
#include "link.h"
#include "lvgl.h"
#include "protocol.h"
#include "ui.h"

static const char *TAG = "panel";

#define HELLO_EVERY_MS 5000
#define ESTOP_EVERY_MS 50
#define PING_EVERY_MS 1000

static QueueHandle_t s_events;
static esp_lcd_panel_handle_t s_panel;
static gt911_t s_touch;
static bool s_touch_ok;

/* The screen as drawn: portrait swaps the panel's axes (see display.c). */
#if CONFIG_PANEL_LANDSCAPE
#define SCREEN_W BOARD_WIDTH
#define SCREEN_H BOARD_HEIGHT
#else
#define SCREEN_W BOARD_HEIGHT
#define SCREEN_H BOARD_WIDTH
#endif

static uint32_t now_ms(void)
{
    return (uint32_t)(esp_timer_get_time() / 1000);
}

static void on_line(const char *line)
{
    panel_event_t event;
    if (protocol_parse(line, &event)) {
        xQueueSend(s_events, &event, 0);
    }
}

static void send_press(const char *key)
{
    char buf[96];
    if (protocol_press(buf, sizeof buf, key) > 0) {
        link_send(buf);
    }
}

static void send_hello(void)
{
    char buf[160];
    const esp_app_desc_t *app = esp_app_get_description();
    if (protocol_hello(buf, sizeof buf, app->version, SCREEN_W, SCREEN_H) > 0) {
        link_send(buf);
    }
}

/* Send the screen as it is drawn, a row per line, for a screenshot. */
static void send_screenshot(void)
{
    lv_draw_buf_t *shot = lv_snapshot_take(lv_screen_active(), LV_COLOR_FORMAT_RGB565);
    if (shot == NULL) {
        ESP_LOGW(TAG, "no memory for a screenshot");
        return;
    }
    int w = shot->header.w, h = shot->header.h;
    static const char HEX[] = "0123456789abcdef";
    size_t size = 128 + (size_t)w * 4;
    char *line = heap_caps_malloc(size, MALLOC_CAP_SPIRAM);
    for (int y = 0; line != NULL && y < h; y++) {
        const uint8_t *row = shot->data + (size_t)y * shot->header.stride;
        int n = snprintf(line, size,
                         "{\"type\":\"screenshot\",\"width\":%d,\"height\":%d,\"row\":%d,"
                         "\"rgb565\":\"",
                         w, h, y);
        for (int i = 0; i < w * 2; i++) {
            line[n++] = HEX[row[i] >> 4];
            line[n++] = HEX[row[i] & 15];
        }
        line[n++] = '"';
        line[n++] = '}';
        line[n] = '\0';
        link_send(line);
    }
    heap_caps_free(line);
    lv_draw_buf_destroy(shot);
}

static void read_touch(lv_indev_t *indev, lv_indev_data_t *data)
{
    (void)indev;
    static bool down;
    static uint16_t x, y;
    if (s_touch_ok) {
        gt911_poll(&s_touch, &down, &x, &y);
    }
    data->state = down ? LV_INDEV_STATE_PRESSED : LV_INDEV_STATE_RELEASED;
    /* The touch controller reports the panel's own (landscape) coordinates. */
#if CONFIG_PANEL_PORTRAIT_RIGHT_UP
    data->point.x = y;
    data->point.y = BOARD_WIDTH - 1 - x;
#elif CONFIG_PANEL_PORTRAIT_LEFT_UP
    data->point.x = BOARD_HEIGHT - 1 - y;
    data->point.y = x;
#else
    data->point.x = x;
    data->point.y = y;
#endif
}

static void start_display(void)
{
    lv_init();
    lv_tick_set_cb(now_ms);
    display_start(s_panel, SCREEN_W, SCREEN_H);
    lv_indev_t *touch = lv_indev_create();
    lv_indev_set_type(touch, LV_INDEV_TYPE_POINTER);
    lv_indev_set_read_cb(touch, read_touch);
}

void app_main(void)
{
    s_events = xQueueCreate(8, sizeof(panel_event_t));

    i2c_master_bus_handle_t bus;
    esp_err_t err;
    ESP_ERROR_CHECK(board_i2c_init(&bus));
    err = expander_init(bus);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "no I/O expander: %s", esp_err_to_name(err));
    }
    err = gt911_init(&s_touch, bus);
    s_touch_ok = err == ESP_OK;
    if (!s_touch_ok) {
        ESP_LOGW(TAG, "no touch controller: %s", esp_err_to_name(err));
    }
    board_estop_init();
    ESP_ERROR_CHECK(board_lcd_init(&s_panel));

    start_display();
    ui_create(send_press);
    ESP_ERROR_CHECK(link_init(on_line));
    send_hello();

    uint32_t last_hello = now_ms();
    uint32_t last_estop_check = 0;
    uint32_t last_ping = 0;
    bool heard = false;
    int estop = -1;
    for (;;) {
        panel_event_t event;
        while (xQueueReceive(s_events, &event, 0) == pdTRUE) {
            heard = heard || event.kind == EVENT_STATE;
            if (event.kind == EVENT_BACKLIGHT) {
                expander_backlight(event.backlight.level);
                continue;
            }
            if (event.kind == EVENT_SCREENSHOT) {
                send_screenshot();
                continue;
            }
            if (event.kind == EVENT_EXPANDER) {
                ESP_LOGI(TAG, "EXIO%d %s", event.expander.pin, event.expander.high ? "high" : "low");
                expander_set(event.expander.pin, event.expander.high);
                continue;
            }
            ui_apply(&event, now_ms());
        }
        uint32_t now = now_ms();
        ui_tick(now);
        if (!heard && now - last_hello > HELLO_EVERY_MS) {
            send_hello();
            last_hello = now;
        }
        if (now - last_ping >= PING_EVERY_MS) {
            last_ping = now;
            char buf[32];
            if (protocol_ping(buf, sizeof buf) > 0) {
                link_send(buf);
            }
        }
        if (now - last_estop_check > ESTOP_EVERY_MS) {
            last_estop_check = now;
            int pressed = board_estop_pressed();
            if (pressed != estop) {
                estop = pressed;
                char buf[64];
                if (protocol_estop(buf, sizeof buf, pressed) > 0) {
                    link_send(buf);
                }
            }
        }
        uint32_t wait = lv_timer_handler();
        vTaskDelay(pdMS_TO_TICKS(wait < 5 ? 5 : (wait > 20 ? 20 : wait)));
    }
}
