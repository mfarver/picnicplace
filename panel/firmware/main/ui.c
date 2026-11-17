/* The panel's screen: the status bar, the position, the core's messages, and the keys.
 *
 * The layout follows docs/hardware/control-panel.md: a 56 px status bar, the position on
 * the left, and a 104 px action bar with the main action on the left and Stop, in red,
 * on the right. Colors are the Studio's dark theme.
 *
 * This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
 */
#include "ui.h"

#include <stdio.h>
#include <string.h>

#include "lvgl.h"

#define STATUS_H 56
#define ACTION_H 104
#define POSITION_W 400
#define PAD 16
#define SILENCE_MS 2000

#define C_BG 0x111418
#define C_CARD 0x1b1f26
#define C_LINE 0x2d333b
#define C_TEXT 0xe6e8eb
#define C_MUTED 0x8b93a1
#define C_GREEN 0x3fb950
#define C_AMBER 0xd29922
#define C_RED 0xf85149
#define C_BLUE 0x4493f8

LV_IMAGE_DECLARE(picnicplace_icon);

static ui_press_cb_t s_on_press;
static uint32_t s_last_state_ms;
static bool s_ever_heard;
static char s_run[16];

static lv_obj_t *s_connected, *s_homed, *s_control, *s_run_label;
static lv_obj_t *s_axis[5];
static lv_obj_t *s_tips;
static lv_obj_t *s_message_title, *s_message_text;
static lv_obj_t *s_level_check;
static lv_obj_t *s_home_btn, *s_pause_btn, *s_pause_label, *s_stop_btn;
static lv_obj_t *s_offline;

static const char *AXES[5] = {"X", "Y", "Z", "A", "B"};

static lv_obj_t *label(lv_obj_t *parent, const lv_font_t *font, uint32_t color, const char *text)
{
    lv_obj_t *l = lv_label_create(parent);
    lv_obj_set_style_text_font(l, font, 0);
    lv_obj_set_style_text_color(l, lv_color_hex(color), 0);
    lv_label_set_text(l, text);
    return l;
}

static lv_obj_t *box(lv_obj_t *parent, int x, int y, int w, int h, uint32_t color)
{
    lv_obj_t *o = lv_obj_create(parent);
    lv_obj_remove_style_all(o);
    lv_obj_set_pos(o, x, y);
    lv_obj_set_size(o, w, h);
    lv_obj_set_style_bg_color(o, lv_color_hex(color), 0);
    lv_obj_set_style_bg_opa(o, LV_OPA_COVER, 0);
    lv_obj_remove_flag(o, LV_OBJ_FLAG_SCROLLABLE);
    return o;
}

static void chip(lv_obj_t *chip, const char *text, uint32_t color)
{
    lv_label_set_text(chip, text);
    lv_obj_set_style_text_color(chip, lv_color_hex(color), 0);
    lv_obj_set_style_border_color(chip, lv_color_hex(color), 0);
}

static lv_obj_t *make_chip(lv_obj_t *parent)
{
    lv_obj_t *c = label(parent, &lv_font_montserrat_16, C_MUTED, "");
    lv_obj_set_style_border_width(c, 1, 0);
    lv_obj_set_style_radius(c, 12, 0);
    lv_obj_set_style_pad_hor(c, 10, 0);
    lv_obj_set_style_pad_ver(c, 4, 0);
    return c;
}

static void pressed(lv_event_t *e)
{
    const char *key = lv_event_get_user_data(e);
    if (strcmp(key, "pause") == 0 && strcmp(s_run, "paused") == 0) {
        key = "resume";
    }
    if (strcmp(key, "home") == 0) {
        /* The check is asked again before every homing. */
        lv_obj_remove_state(s_level_check, LV_STATE_CHECKED);
        lv_obj_add_state(s_home_btn, LV_STATE_DISABLED);
    }
    if (s_on_press != NULL) {
        s_on_press(key);
    }
}

static void level_checked(lv_event_t *e)
{
    (void)e;
    if (lv_obj_has_state(s_level_check, LV_STATE_CHECKED)) {
        lv_obj_remove_state(s_home_btn, LV_STATE_DISABLED);
    } else {
        lv_obj_add_state(s_home_btn, LV_STATE_DISABLED);
    }
}

static lv_obj_t *button(lv_obj_t *parent, int x, int w, uint32_t color, const char *text,
                        const char *key, lv_obj_t **text_label)
{
    lv_obj_t *b = lv_button_create(parent);
    lv_obj_set_pos(b, x, (ACTION_H - 76) / 2);
    lv_obj_set_size(b, w, 76);
    lv_obj_set_style_bg_color(b, lv_color_hex(color), 0);
    lv_obj_set_style_bg_color(b, lv_color_hex(C_LINE), LV_STATE_DISABLED);
    lv_obj_set_style_radius(b, 10, 0);
    lv_obj_t *l = label(b, &lv_font_montserrat_28, C_TEXT, text);
    lv_obj_center(l);
    lv_obj_add_event_cb(b, pressed, LV_EVENT_CLICKED, (void *)key);
    if (text_label != NULL) {
        *text_label = l;
    }
    return b;
}

void ui_create(ui_press_cb_t on_press)
{
    s_on_press = on_press;
    lv_obj_t *screen = lv_screen_active();
    lv_obj_set_style_bg_color(screen, lv_color_hex(C_BG), 0);
    lv_obj_remove_flag(screen, LV_OBJ_FLAG_SCROLLABLE);
    int w = lv_display_get_horizontal_resolution(NULL);
    int h = lv_display_get_vertical_resolution(NULL);

    /* The status bar. */
    lv_obj_t *bar = box(screen, 0, 0, w, STATUS_H, C_CARD);
    lv_obj_t *icon = lv_image_create(bar);
    lv_image_set_src(icon, &picnicplace_icon);
    lv_obj_align(icon, LV_ALIGN_LEFT_MID, PAD, 0);
    lv_obj_t *name = label(bar, &lv_font_montserrat_20, C_TEXT, "PicnicPlace");
    lv_obj_align_to(name, icon, LV_ALIGN_OUT_RIGHT_MID, 10, 0);
    s_connected = make_chip(bar);
    lv_obj_align_to(s_connected, name, LV_ALIGN_OUT_RIGHT_MID, 28, 0);
    s_homed = make_chip(bar);
    lv_obj_align_to(s_homed, s_connected, LV_ALIGN_OUT_RIGHT_MID, 12, 0);
    s_control = make_chip(bar);
    lv_obj_align_to(s_control, s_homed, LV_ALIGN_OUT_RIGHT_MID, 12, 0);
    s_run_label = label(bar, &lv_font_montserrat_20, C_MUTED, "");
    lv_obj_align(s_run_label, LV_ALIGN_RIGHT_MID, -PAD, 0);

    /* The position. */
    int top = STATUS_H + PAD;
    for (int i = 0; i < 3; i++) {
        lv_obj_t *axis = label(screen, &lv_font_montserrat_28, C_MUTED, AXES[i]);
        lv_obj_set_pos(axis, PAD + 8, top + 14 + i * 72);
        s_axis[i] = label(screen, &lv_font_montserrat_48, C_TEXT, "-");
        lv_obj_set_width(s_axis[i], 280);
        lv_obj_set_style_text_align(s_axis[i], LV_TEXT_ALIGN_RIGHT, 0);
        lv_obj_set_pos(s_axis[i], 70, top + i * 72);
    }
    for (int i = 3; i < 5; i++) {
        s_axis[i] = label(screen, &lv_font_montserrat_20, C_MUTED, "");
        lv_obj_set_pos(s_axis[i], PAD + 8, top + 230 + (i - 3) * 32);
    }
    s_tips = label(screen, &lv_font_montserrat_20, C_MUTED, "");
    lv_obj_set_pos(s_tips, PAD + 8, top + 310);

    /* The core's messages. */
    lv_obj_t *card = box(screen, POSITION_W, top, w - POSITION_W - PAD, h - top - ACTION_H - PAD,
                         C_CARD);
    lv_obj_set_style_radius(card, 10, 0);
    lv_obj_set_style_pad_all(card, PAD, 0);
    s_message_title = label(card, &lv_font_montserrat_20, C_MUTED, "Messages");
    lv_obj_set_pos(s_message_title, 0, 0);
    s_message_text = label(card, &lv_font_montserrat_20, C_TEXT, "Nothing yet.");
    lv_label_set_long_mode(s_message_text, LV_LABEL_LONG_WRAP);
    lv_obj_set_width(s_message_text, lv_pct(100));
    lv_obj_set_pos(s_message_text, 0, 36);
    s_level_check = lv_checkbox_create(card);
    lv_checkbox_set_text(s_level_check, "Both nozzles are level and raised");
    lv_obj_set_style_text_font(s_level_check, &lv_font_montserrat_20, 0);
    lv_obj_set_style_text_color(s_level_check, lv_color_hex(C_TEXT), 0);
    lv_obj_align(s_level_check, LV_ALIGN_BOTTOM_LEFT, 0, 0);
    lv_obj_add_event_cb(s_level_check, level_checked, LV_EVENT_VALUE_CHANGED, NULL);

    /* The action bar. */
    lv_obj_t *actions = box(screen, 0, h - ACTION_H, w, ACTION_H, C_CARD);
    s_home_btn = button(actions, PAD, 300, C_BLUE, "Home", "home", NULL);
    lv_obj_add_state(s_home_btn, LV_STATE_DISABLED);
    s_pause_btn = button(actions, PAD + 300 + PAD, 300, C_AMBER, "Pause", "pause", &s_pause_label);
    lv_obj_add_state(s_pause_btn, LV_STATE_DISABLED);
    s_stop_btn = button(actions, w - PAD - 300, 300, C_RED, "Stop", "stop", NULL);

    /* Shown while the core is silent. */
    s_offline = box(screen, 0, 0, w, h, C_BG);
    lv_obj_set_style_bg_opa(s_offline, LV_OPA_90, 0);
    lv_obj_t *offline = label(s_offline, &lv_font_montserrat_28, C_TEXT,
                              "No connection to the core");
    lv_obj_center(offline);
}

static void apply_state(const panel_state_t *s)
{
    chip(s_connected, s->connected ? "Connected" : "Not connected",
         s->connected ? C_GREEN : C_AMBER);
    chip(s_homed, s->homed ? "Homed" : "Not homed", s->homed ? C_GREEN : C_AMBER);
    if (s->control[0] == '\0') {
        chip(s_control, "No one has control", C_MUTED);
    } else if (strcmp(s->control, "panel") == 0) {
        chip(s_control, "This panel has control", C_GREEN);
    } else {
        char text[64];
        snprintf(text, sizeof text, "%s has control", s->control);
        chip(s_control, text, C_BLUE);
    }
    lv_obj_align_to(s_homed, s_connected, LV_ALIGN_OUT_RIGHT_MID, 12, 0);
    lv_obj_align_to(s_control, s_homed, LV_ALIGN_OUT_RIGHT_MID, 12, 0);

    snprintf(s_run, sizeof s_run, "%s", s->run);
    const char *run = "Ready";
    uint32_t color = C_MUTED;
    if (strcmp(s->run, "running") == 0) {
        run = "Running";
        color = C_GREEN;
    } else if (strcmp(s->run, "paused") == 0) {
        run = "Paused";
        color = C_AMBER;
    } else if (strcmp(s->run, "waiting") == 0) {
        run = "Waiting for you";
        color = C_AMBER;
    }
    lv_label_set_text(s_run_label, run);
    lv_obj_set_style_text_color(s_run_label, lv_color_hex(color), 0);
    lv_label_set_text(s_pause_label, strcmp(s->run, "paused") == 0 ? "Resume" : "Pause");
    if (strcmp(s->run, "running") == 0 || strcmp(s->run, "paused") == 0) {
        lv_obj_remove_state(s_pause_btn, LV_STATE_DISABLED);
    } else {
        lv_obj_add_state(s_pause_btn, LV_STATE_DISABLED);
    }

    if (s->has_position) {
        double v[5] = {s->x, s->y, s->z, s->a, s->b};
        char text[32];
        for (int i = 0; i < 3; i++) {
            snprintf(text, sizeof text, "%.2f", v[i]);
            lv_label_set_text(s_axis[i], text);
        }
        for (int i = 3; i < 5; i++) {
            snprintf(text, sizeof text, "%s  %.1f\xc2\xb0", AXES[i], v[i]);
            lv_label_set_text(s_axis[i], text);
        }
    }
    char tips[160] = "";
    for (int i = 0; i < s->tip_count; i++) {
        size_t used = strlen(tips);
        snprintf(tips + used, sizeof tips - used, "%s: %s\n", s->tip_nozzle[i],
                 s->tip_name[i][0] ? s->tip_name[i] : "no tip");
    }
    lv_label_set_text(s_tips, tips);
}

void ui_apply(const panel_event_t *event, uint32_t now_ms)
{
    if (event->kind == EVENT_STATE) {
        s_last_state_ms = now_ms;
        s_ever_heard = true;
        lv_obj_add_flag(s_offline, LV_OBJ_FLAG_HIDDEN);
        apply_state(&event->state);
    } else if (event->kind == EVENT_MESSAGE) {
        bool error = strcmp(event->message.level, "error") == 0;
        lv_label_set_text(s_message_title, error ? "Error" : "Notice");
        lv_obj_set_style_text_color(s_message_title, lv_color_hex(error ? C_RED : C_MUTED), 0);
        lv_label_set_text(s_message_text, event->message.text);
    }
}

void ui_tick(uint32_t now_ms)
{
    if (!s_ever_heard || now_ms - s_last_state_ms > SILENCE_MS) {
        lv_obj_remove_flag(s_offline, LV_OBJ_FLAG_HIDDEN);
    }
}
