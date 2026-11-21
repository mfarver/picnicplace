/* LVGL on the panel's screen: turned to portrait, and double-buffered so nothing is seen
 * half drawn.
 *
 * The RGB panel scans one of two frame buffers in PSRAM while LVGL's drawing goes into
 * the other, turned from the screen as drawn (portrait) to the panel's own landscape
 * pixels. When a refresh is complete, the two swap at the start of the next frame, and
 * the areas just drawn are copied into the buffer now hidden, so it is current for the
 * next refresh. With one frame buffer, the seven-segment readout was seen being redrawn
 * as a wipe down the digits.
 *
 * This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
 */
#include "display.h"

#include <string.h>

#include "board.h"
#include "esp_heap_caps.h"
#include "esp_lcd_panel_rgb.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "sdkconfig.h"

static const char *TAG = "display";

#define DRAW_LINES 60
#define MAX_DIRTY 16
#define VSYNC_WAIT_MS 100

static esp_lcd_panel_handle_t s_panel;
static uint16_t *s_fb[2];
static int s_back = 1; /* the panel shows frame buffer 0 first */
static SemaphoreHandle_t s_vsync;
static lv_area_t s_dirty[MAX_DIRTY];
static int s_dirty_count;
static bool s_dirty_all;

static bool IRAM_ATTR on_vsync(esp_lcd_panel_handle_t panel,
                               const esp_lcd_rgb_panel_event_data_t *data, void *ctx)
{
    BaseType_t woken = pdFALSE;
    xSemaphoreGiveFromISR(s_vsync, &woken);
    return woken == pdTRUE;
}

/* A drawn area's rectangle in the panel's own pixels (inclusive). */
static void to_panel(const lv_area_t *a, int *x0, int *y0, int *x1, int *y1)
{
#if CONFIG_PANEL_PORTRAIT_RIGHT_UP
    /* Drawn (x, y) is the panel's (width - 1 - y, x). */
    *x0 = BOARD_WIDTH - 1 - a->y2;
    *x1 = BOARD_WIDTH - 1 - a->y1;
    *y0 = a->x1;
    *y1 = a->x2;
#elif CONFIG_PANEL_PORTRAIT_LEFT_UP
    /* Drawn (x, y) is the panel's (y, height - 1 - x). */
    *x0 = a->y1;
    *x1 = a->y2;
    *y0 = BOARD_HEIGHT - 1 - a->x2;
    *y1 = BOARD_HEIGHT - 1 - a->x1;
#else
    *x0 = a->x1;
    *x1 = a->x2;
    *y0 = a->y1;
    *y1 = a->y2;
#endif
}

/* Copy LVGL's drawing of an area into a frame buffer, turned to the panel's pixels. */
static void put(uint16_t *fb, const lv_area_t *a, const uint16_t *src)
{
    int w = lv_area_get_width(a);
    for (int y = a->y1; y <= a->y2; y++) {
        const uint16_t *row = src + (size_t)(y - a->y1) * w;
#if CONFIG_PANEL_PORTRAIT_RIGHT_UP
        uint16_t *to = fb + (size_t)a->x1 * BOARD_WIDTH + (BOARD_WIDTH - 1 - y);
        for (int x = 0; x < w; x++, to += BOARD_WIDTH) {
            *to = row[x];
        }
#elif CONFIG_PANEL_PORTRAIT_LEFT_UP
        uint16_t *to = fb + (size_t)(BOARD_HEIGHT - 1 - a->x1) * BOARD_WIDTH + y;
        for (int x = 0; x < w; x++, to -= BOARD_WIDTH) {
            *to = row[x];
        }
#else
        memcpy(fb + (size_t)y * BOARD_WIDTH + a->x1, row, (size_t)w * 2);
#endif
    }
}

/* Copy a drawn area, in the panel's pixels, from one frame buffer to the other. */
static void sync(uint16_t *to, const uint16_t *from, const lv_area_t *a)
{
    int x0, y0, x1, y1;
    to_panel(a, &x0, &y0, &x1, &y1);
    for (int y = y0; y <= y1; y++) {
        size_t at = (size_t)y * BOARD_WIDTH + x0;
        memcpy(to + at, from + at, (size_t)(x1 - x0 + 1) * 2);
    }
}

static void remember(const lv_area_t *a)
{
    if (s_dirty_count < MAX_DIRTY) {
        s_dirty[s_dirty_count++] = *a;
    } else {
        s_dirty_all = true;
    }
}

static void flush(lv_display_t *display, const lv_area_t *area, uint8_t *pixels)
{
    put(s_fb[s_back], area, (const uint16_t *)pixels);
    remember(area);
    if (lv_display_flush_is_last(display)) {
        /* Show the finished buffer from the next frame, and wait until it is shown. */
        xSemaphoreTake(s_vsync, 0);
        esp_lcd_panel_draw_bitmap(s_panel, 0, 0, BOARD_WIDTH, BOARD_HEIGHT, s_fb[s_back]);
        xSemaphoreTake(s_vsync, pdMS_TO_TICKS(VSYNC_WAIT_MS));
        int shown = s_back;
        s_back ^= 1;
        if (s_dirty_all) {
            memcpy(s_fb[s_back], s_fb[shown], (size_t)BOARD_WIDTH * BOARD_HEIGHT * 2);
        } else {
            for (int i = 0; i < s_dirty_count; i++) {
                sync(s_fb[s_back], s_fb[shown], &s_dirty[i]);
            }
        }
        s_dirty_count = 0;
        s_dirty_all = false;
    }
    lv_display_flush_ready(display);
}

lv_display_t *display_start(esp_lcd_panel_handle_t panel, int width, int height)
{
    s_panel = panel;
    s_vsync = xSemaphoreCreateBinary();
    void *fb0 = NULL, *fb1 = NULL;
    ESP_ERROR_CHECK(esp_lcd_rgb_panel_get_frame_buffer(panel, 2, &fb0, &fb1));
    s_fb[0] = fb0;
    s_fb[1] = fb1;
    esp_lcd_rgb_panel_event_callbacks_t callbacks = {.on_vsync = on_vsync};
    ESP_ERROR_CHECK(esp_lcd_rgb_panel_register_event_callbacks(panel, &callbacks, NULL));

    lv_display_t *display = lv_display_create(width, height);
    lv_display_set_color_format(display, LV_COLOR_FORMAT_RGB565);
    size_t size = (size_t)width * DRAW_LINES * 2;
    void *a = heap_caps_malloc(size, MALLOC_CAP_SPIRAM);
    void *b = heap_caps_malloc(size, MALLOC_CAP_SPIRAM);
    lv_display_set_buffers(display, a, b, size, LV_DISPLAY_RENDER_MODE_PARTIAL);
    lv_display_set_flush_cb(display, flush);
    ESP_LOGI(TAG, "%d x %d, double-buffered", width, height);
    return display;
}
