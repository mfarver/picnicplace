/* A seven-segment readout, drawn as segments rather than from a font.
 *
 * Segments, as on any seven-segment display:
 *        a
 *      f   b
 *        g
 *      e   c
 *        d
 * Each digit is a cell 0.55 of its height wide; a decimal point takes a narrow cell of
 * its own. The digits lean slightly, as LED displays often do.
 *
 * This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
 */
#include "seg7.h"

#include <string.h>

#define MAX_TEXT 16
#define WIDTH 0.55f   /* a digit cell's width, in digit heights */
#define THICK 0.11f   /* a segment's thickness */
#define GAP 0.02f     /* between segments */
#define SPACING 0.14f /* between digits */
#define POINT 0.22f   /* a decimal point's cell */
#define SLANT 0.06f   /* how far the top leans right, in digit heights */
#define UNLIT_OPA LV_OPA_10

typedef struct {
    int height;
    lv_color_t color;
    char text[MAX_TEXT];
} seg7_t;

/* Which segments each character lights: bits a (0) to g (6). */
static uint8_t segments(char c)
{
    static const uint8_t DIGITS[10] = {0x3F, 0x06, 0x5B, 0x4F, 0x66, 0x6D, 0x7D, 0x07, 0x7F, 0x6F};
    if (c >= '0' && c <= '9') {
        return DIGITS[c - '0'];
    }
    return c == '-' ? 0x40 : 0;
}

static void draw_bar(lv_layer_t *layer, lv_draw_rect_dsc_t *dsc, float x0, float y0, float x1,
                     float y1)
{
    lv_area_t area = {(int32_t)x0, (int32_t)y0, (int32_t)x1, (int32_t)y1};
    lv_draw_rect(layer, dsc, &area);
}

/* One digit cell, its top-left corner at (x, y). */
static void draw_digit(lv_layer_t *layer, const seg7_t *s, float x, float y, uint8_t lit)
{
    float h = (float)s->height;
    float w = WIDTH * h, t = THICK * h, g = GAP * h, half = h / 2;
    lv_draw_rect_dsc_t dsc;
    lv_draw_rect_dsc_init(&dsc);
    dsc.bg_color = s->color;
    dsc.radius = (int32_t)(t / 2);
    /* The lean: a segment's x moves right as it rises. */
#define LEAN(yy) (SLANT * (h - (yy)))
    struct {
        float x0, y0, x1, y1;
    } bars[7] = {
        {t + g, 0, w - t - g, t},                       /* a */
        {w - t, t / 2 + g, w, half - g},                /* b */
        {w - t, half + g, w, h - t / 2 - g},            /* c */
        {t + g, h - t, w - t - g, h},                   /* d */
        {0, half + g, t, h - t / 2 - g},                /* e */
        {0, t / 2 + g, t, half - g},                    /* f */
        {t + g, half - t / 2, w - t - g, half + t / 2}, /* g */
    };
    for (int i = 0; i < 7; i++) {
        dsc.bg_opa = (lit >> i) & 1 ? LV_OPA_COVER : UNLIT_OPA;
        float mid = (bars[i].y0 + bars[i].y1) / 2;
        float dx = LEAN(mid);
        draw_bar(layer, &dsc, x + bars[i].x0 + dx, y + bars[i].y0, x + bars[i].x1 + dx,
                 y + bars[i].y1);
    }
#undef LEAN
}

static void draw_point(lv_layer_t *layer, const seg7_t *s, float x, float y)
{
    float h = (float)s->height, t = THICK * h;
    lv_draw_rect_dsc_t dsc;
    lv_draw_rect_dsc_init(&dsc);
    dsc.bg_color = s->color;
    dsc.bg_opa = LV_OPA_COVER;
    dsc.radius = (int32_t)(t / 2);
    float cx = x + (POINT * h - t) / 2;
    draw_bar(layer, &dsc, cx, y + h - t, cx + t, y + h);
}

/* The width the text takes, in pixels. */
static float text_width(const seg7_t *s)
{
    float h = (float)s->height, total = 0;
    for (const char *c = s->text; *c; c++) {
        total += *c == '.' ? POINT * h : (WIDTH + SPACING) * h;
    }
    return total;
}

static void draw(lv_event_t *e)
{
    lv_obj_t *obj = lv_event_get_target(e);
    const seg7_t *s = lv_obj_get_user_data(obj);
    lv_layer_t *layer = lv_event_get_layer(e);
    lv_area_t area;
    lv_obj_get_coords(obj, &area);
    float h = (float)s->height;
    float x = (float)area.x2 + 1 - text_width(s) - SLANT * h; /* room for the lean */
    float y = (float)area.y1 + ((float)lv_area_get_height(&area) - h) / 2;
    for (const char *c = s->text; *c; c++) {
        if (*c == '.') {
            draw_point(layer, s, x, y);
            x += POINT * h;
        } else {
            draw_digit(layer, s, x, y, segments(*c));
            x += (WIDTH + SPACING) * h;
        }
    }
}

static void deleted(lv_event_t *e)
{
    lv_free(lv_obj_get_user_data(lv_event_get_target(e)));
}

lv_obj_t *seg7_create(lv_obj_t *parent, int digit_height, lv_color_t color)
{
    lv_obj_t *obj = lv_obj_create(parent);
    lv_obj_remove_style_all(obj);
    lv_obj_remove_flag(obj, LV_OBJ_FLAG_SCROLLABLE | LV_OBJ_FLAG_CLICKABLE);
    lv_obj_set_height(obj, digit_height + 4);
    seg7_t *s = lv_malloc(sizeof(seg7_t));
    memset(s, 0, sizeof *s);
    s->height = digit_height;
    s->color = color;
    lv_obj_set_user_data(obj, s);
    lv_obj_add_event_cb(obj, draw, LV_EVENT_DRAW_MAIN, NULL);
    lv_obj_add_event_cb(obj, deleted, LV_EVENT_DELETE, NULL);
    return obj;
}

void seg7_set_text(lv_obj_t *readout, const char *text)
{
    seg7_t *s = lv_obj_get_user_data(readout);
    if (strncmp(s->text, text, MAX_TEXT - 1) == 0) {
        return;
    }
    strncpy(s->text, text, MAX_TEXT - 1);
    s->text[MAX_TEXT - 1] = '\0';
    lv_obj_invalidate(readout);
}
