/* The messages the panel and the core exchange: JSON, one message per line.
 *
 * This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
 */
#include "protocol.h"

#include <stdio.h>
#include <string.h>

#include "cJSON.h"

static void copy_string(char *dst, size_t size, const cJSON *item)
{
    dst[0] = '\0';
    if (cJSON_IsString(item) && item->valuestring != NULL) {
        snprintf(dst, size, "%s", item->valuestring);
    }
}

static double number(const cJSON *obj, const char *name)
{
    const cJSON *item = cJSON_GetObjectItemCaseSensitive(obj, name);
    return cJSON_IsNumber(item) ? item->valuedouble : 0.0;
}

static void parse_state(const cJSON *root, panel_state_t *s)
{
    memset(s, 0, sizeof *s);
    s->connected = cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(root, "connected"));
    s->homed = cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(root, "homed"));
    copy_string(s->control, sizeof s->control, cJSON_GetObjectItemCaseSensitive(root, "control"));
    copy_string(s->run, sizeof s->run, cJSON_GetObjectItemCaseSensitive(root, "run"));
    const cJSON *pos = cJSON_GetObjectItemCaseSensitive(root, "position");
    if (cJSON_IsObject(pos)) {
        s->has_position = true;
        s->x = number(pos, "X");
        s->y = number(pos, "Y");
        s->z = number(pos, "Z");
        s->a = number(pos, "A");
        s->b = number(pos, "B");
    }
    const cJSON *tips = cJSON_GetObjectItemCaseSensitive(root, "tips");
    const cJSON *tip = NULL;
    cJSON_ArrayForEach(tip, tips)
    {
        if (s->tip_count >= PROTOCOL_MAX_TIPS || tip->string == NULL) {
            break;
        }
        snprintf(s->tip_nozzle[s->tip_count], sizeof s->tip_nozzle[0], "%s", tip->string);
        copy_string(s->tip_name[s->tip_count], sizeof s->tip_name[0], tip);
        s->tip_count++;
    }
}

bool protocol_parse(const char *line, panel_event_t *out)
{
    out->kind = EVENT_NONE;
    if (line[0] != '{') {
        return false;
    }
    cJSON *root = cJSON_Parse(line);
    if (root == NULL) {
        return false;
    }
    const cJSON *type = cJSON_GetObjectItemCaseSensitive(root, "type");
    if (cJSON_IsString(type) && strcmp(type->valuestring, "state") == 0) {
        out->kind = EVENT_STATE;
        parse_state(root, &out->state);
    } else if (cJSON_IsString(type) && strcmp(type->valuestring, "message") == 0) {
        out->kind = EVENT_MESSAGE;
        copy_string(out->message.level, sizeof out->message.level,
                    cJSON_GetObjectItemCaseSensitive(root, "level"));
        copy_string(out->message.text, sizeof out->message.text,
                    cJSON_GetObjectItemCaseSensitive(root, "text"));
    } else if (cJSON_IsString(type) && strcmp(type->valuestring, "backlight") == 0) {
        out->kind = EVENT_BACKLIGHT;
        double level = number(root, "level");
        out->backlight.level = level < 0 ? 0 : (level > 100 ? 100 : (int)level);
    } else if (cJSON_IsString(type) && strcmp(type->valuestring, "screenshot") == 0) {
        out->kind = EVENT_SCREENSHOT;
    } else if (cJSON_IsString(type) && strcmp(type->valuestring, "expander") == 0) {
        int pin = (int)number(root, "pin");
        if (pin >= 0 && pin <= 7) {
            out->kind = EVENT_EXPANDER;
            out->expander.pin = pin;
            out->expander.high = cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(root, "high"));
        }
    }
    cJSON_Delete(root);
    return out->kind != EVENT_NONE;
}

static size_t print(cJSON *root, char *buf, size_t size)
{
    bool ok = root != NULL && cJSON_PrintPreallocated(root, buf, (int)size, false);
    cJSON_Delete(root);
    return ok ? strlen(buf) : 0;
}

size_t protocol_hello(char *buf, size_t size, const char *firmware, int width, int height)
{
    cJSON *root = cJSON_CreateObject();
    cJSON_AddStringToObject(root, "type", "hello");
    cJSON_AddStringToObject(root, "firmware", firmware);
    cJSON *screen = cJSON_AddObjectToObject(root, "screen");
    cJSON_AddNumberToObject(screen, "width", width);
    cJSON_AddNumberToObject(screen, "height", height);
    return print(root, buf, size);
}

size_t protocol_press(char *buf, size_t size, const char *key)
{
    cJSON *root = cJSON_CreateObject();
    cJSON_AddStringToObject(root, "type", "press");
    cJSON_AddStringToObject(root, "key", key);
    return print(root, buf, size);
}

size_t protocol_ping(char *buf, size_t size)
{
    cJSON *root = cJSON_CreateObject();
    cJSON_AddStringToObject(root, "type", "ping");
    return print(root, buf, size);
}

size_t protocol_estop(char *buf, size_t size, bool pressed)
{
    cJSON *root = cJSON_CreateObject();
    cJSON_AddStringToObject(root, "type", "estop");
    cJSON_AddBoolToObject(root, "pressed", pressed);
    return print(root, buf, size);
}
