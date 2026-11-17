/* The messages the panel and the core exchange: JSON, one message per line.
 *
 * From the core (the panel draws what the core last sent, and keeps no state of its own):
 *   {"type": "state", "connected": true, "homed": false, "control": "Studio",
 *    "run": "ready", "position": {"X": 1.0, "Y": 2.0, "Z": 0.0, "A": 0.0, "B": 0.0},
 *    "tips": {"Left nozzle": "N045", "Right nozzle": "N08"}}
 *       at least once a second. "control" is who holds the control lease ("panel" when
 *       the panel does, null when no one does); "run" is ready, running, paused or waiting.
 *   {"type": "message", "level": "error", "text": "..."}
 *       an error or notice, in the Studio's words.
 *   {"type": "backlight", "level": 60}
 *       the backlight's brightness, 0 to 100.
 *   {"type": "expander", "pin": 5, "high": true}
 *       for development only: drive one of the I/O expander's pins.
 *
 * From the panel:
 *   {"type": "hello", "firmware": "0.1.0", "screen": {"width": 1024, "height": 600}}
 *       when it starts, and every five seconds until the core answers
 *   {"type": "ping"}                       every second, so the core knows it is there
 *   {"type": "press", "key": "home"}       home, start, pause, resume or stop
 *   {"type": "estop", "pressed": true}     whenever the E-stop's sensing pole changes
 *
 * This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>

#define PROTOCOL_MAX_TIPS 4

typedef struct {
    bool connected;
    bool homed;
    char control[32]; /* empty when no one holds control */
    char run[16];
    bool has_position;
    double x, y, z, a, b;
    int tip_count;
    char tip_nozzle[PROTOCOL_MAX_TIPS][24];
    char tip_name[PROTOCOL_MAX_TIPS][16];
} panel_state_t;

typedef struct {
    char level[16];
    char text[256];
} panel_message_t;

typedef struct {
    int level; /* 0-100 */
} panel_backlight_t;

typedef struct {
    int pin; /* 0-7 */
    bool high;
} panel_expander_t;

typedef enum {
    EVENT_NONE,
    EVENT_STATE,
    EVENT_MESSAGE,
    EVENT_BACKLIGHT,
    EVENT_EXPANDER,
} panel_event_kind_t;

typedef struct {
    panel_event_kind_t kind;
    union {
        panel_state_t state;
        panel_message_t message;
        panel_backlight_t backlight;
        panel_expander_t expander;
    };
} panel_event_t;

/* Read one line from the core. False if it is not a message the panel knows (log lines
 * and unknown types are ignored, so the core can add messages without breaking it). */
bool protocol_parse(const char *line, panel_event_t *out);

/* Write a message to the panel's buffer, without its newline: its length, or 0. */
size_t protocol_hello(char *buf, size_t size, const char *firmware, int width, int height);
size_t protocol_press(char *buf, size_t size, const char *key);
size_t protocol_ping(char *buf, size_t size);
size_t protocol_estop(char *buf, size_t size, bool pressed);
