/* The PicnicPlace control panel: a touchscreen that shows the machine's state and sends
 * the operator's keys to the core, over a serial link. It keeps no state of its own and
 * never talks to the machine's controller: every key is checked by the core, as a click
 * in the Studio is. See docs/hardware/control-panel.md.
 *
 * This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
 */
#include <stdio.h>

#include "board.h"
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

#define DRAW_LINES 60
#define HELLO_EVERY_MS 5000
#define ESTOP_EVERY_MS 50
#define PING_EVERY_MS 1000

static QueueHandle_t s_events;
static esp_lcd_panel_handle_t s_panel;
static gt911_t s_touch;
static bool s_touch_ok;

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
    if (protocol_hello(buf, sizeof buf, app->version, BOARD_WIDTH, BOARD_HEIGHT) > 0) {
        link_send(buf);
    }
}

static void flush(lv_display_t *display, const lv_area_t *area, uint8_t *pixels)
{
    esp_lcd_panel_draw_bitmap(s_panel, area->x1, area->y1, area->x2 + 1, area->y2 + 1, pixels);
    lv_display_flush_ready(display);
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
    data->point.x = x;
    data->point.y = y;
}

static void start_display(void)
{
    lv_init();
    lv_tick_set_cb(now_ms);
    lv_display_t *display = lv_display_create(BOARD_WIDTH, BOARD_HEIGHT);
    lv_display_set_color_format(display, LV_COLOR_FORMAT_RGB565);
    size_t size = BOARD_WIDTH * DRAW_LINES * 2;
    void *a = heap_caps_malloc(size, MALLOC_CAP_SPIRAM);
    void *b = heap_caps_malloc(size, MALLOC_CAP_SPIRAM);
    lv_display_set_buffers(display, a, b, size, LV_DISPLAY_RENDER_MODE_PARTIAL);
    lv_display_set_flush_cb(display, flush);
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
