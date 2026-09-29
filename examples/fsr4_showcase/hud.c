/* Copyright (C) 2026 BlackBearReloaded
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * PS5 FSR4 Showcase HUD drawing. The glyphs come from fsr4_showcase_font.h,
 * which tools/build_fsr4_showcase.py rasterizes from DejaVu Sans at build time.
 */
#include "hud.h"
#include "fsr4_showcase_font.h"

#include <math.h>
#include <stddef.h>
#include <string.h>

enum { MAX_RECTS = 64 };
static struct hud_rect drawn[2][MAX_RECTS];  /* by the previous and the current redraw */
static int drawn_count[2], current;

static void touch(int x0, int y0, int x1, int y1)
{
    x0 = x0 < 0 ? 0 : x0; y0 = y0 < 0 ? 0 : y0;
    x1 = x1 > HUD_W ? HUD_W : x1; y1 = y1 > HUD_H ? HUD_H : y1;
    if (x0 >= x1 || y0 >= y1) return;
    struct hud_rect *list = drawn[current];
    int *count = &drawn_count[current];
    if (*count < MAX_RECTS) {
        list[(*count)++] = (struct hud_rect){x0, y0, x1, y1};
    } else {  /* out of entries: grow the last one */
        struct hud_rect *r = &list[MAX_RECTS - 1];
        if (x0 < r->x0) r->x0 = x0;
        if (y0 < r->y0) r->y0 = y0;
        if (x1 > r->x1) r->x1 = x1;
        if (y1 > r->y1) r->y1 = y1;
    }
}

void hud_begin(uint32_t *overlay)
{
    current ^= 1;
    drawn_count[current] = 0;
    for (int i = 0; i < drawn_count[current ^ 1]; ++i) {
        const struct hud_rect *r = &drawn[current ^ 1][i];
        for (int y = r->y0; y < r->y1; ++y)
            memset(overlay + (size_t)y * HUD_W + r->x0, 0, (size_t)(r->x1 - r->x0) * 4);
    }
}

int hud_damage(struct hud_rect *rects, int max)
{
    int n = 0;
    for (int list = 0; list < 2; ++list)
        for (int i = 0; i < drawn_count[list] && n < max; ++i) rects[n++] = drawn[list][i];
    return n;
}

static const struct hud_font *font_of(enum hud_face face)
{
    return face == HUD_TITLE ? &hud_font_title : face == HUD_BODY ? &hud_font_body : &hud_font_small;
}

/* Premultiplied source over the overlay pixel. */
static void blend(uint32_t *pixel, uint32_t r, uint32_t g, uint32_t b, uint32_t a)
{
    const uint32_t d = *pixel, keep = 255 - a;
    if (!d) { *pixel = HUD_RGBA(r, g, b, a); return; }
    const uint32_t dr = d & 0xff, dg = (d >> 8) & 0xff, db = (d >> 16) & 0xff, da = d >> 24;
    *pixel = HUD_RGBA(r + (dr * keep + 127) / 255, g + (dg * keep + 127) / 255, b + (db * keep + 127) / 255,
                      a + (da * keep + 127) / 255);
}

/* Blends straight-alpha rgba with an extra coverage in [0, 255]. */
static void blend_color(uint32_t *pixel, uint32_t rgba, uint32_t coverage)
{
    const uint32_t a = ((rgba >> 24) * coverage + 127) / 255;
    blend(pixel, ((rgba & 0xff) * a + 127) / 255, (((rgba >> 8) & 0xff) * a + 127) / 255,
          (((rgba >> 16) & 0xff) * a + 127) / 255, a);
}

void hud_fill(uint32_t *overlay, int x, int y, int w, int h, int radius, uint32_t rgba)
{
    touch(x, y, x + w, y + h);
    const uint32_t a = rgba >> 24;  /* the premultiplied color of full coverage */
    const uint32_t full = HUD_RGBA(((rgba & 0xff) * a + 127) / 255, (((rgba >> 8) & 0xff) * a + 127) / 255,
                                   (((rgba >> 16) & 0xff) * a + 127) / 255, a);
    for (int j = y < 0 ? 0 : y; j < y + h && j < HUD_H; ++j)
        for (int i = x < 0 ? 0 : x; i < x + w && i < HUD_W; ++i) {
            uint32_t *pixel = &overlay[j * HUD_W + i];
            const int left = i < x + radius, right = i >= x + w - radius;
            const int top = j < y + radius, bottom = j >= y + h - radius;
            if (!((left || right) && (top || bottom))) {
                if (*pixel) blend_color(pixel, rgba, 255);
                else *pixel = full;
                continue;
            }
            /* A corner: coverage from the distance outside its circle. */
            const float cx = (float)(left ? x + radius : x + w - radius);
            const float cy = (float)(top ? y + radius : y + h - radius);
            const float d = hypotf((float)i + 0.5f - cx, (float)j + 0.5f - cy) - (float)radius;
            const float coverage = d <= -0.5f ? 1.0f : d >= 0.5f ? 0.0f : 0.5f - d;
            if (coverage > 0.0f) blend_color(pixel, rgba, (uint32_t)(coverage * 255.0f + 0.5f));
        }
}

/* The next code point of UTF-8 text; malformed bytes read as '?'. */
static uint32_t next_codepoint(const char **text)
{
    const unsigned char *s = (const unsigned char *)*text;
    uint32_t c = s[0];
    int length = c < 0x80 ? 1 : (c & 0xe0) == 0xc0 ? 2 : (c & 0xf0) == 0xe0 ? 3 : 0;
    if (!length) { *text += 1; return '?'; }
    c &= length == 1 ? 0x7f : length == 2 ? 0x1f : 0x0f;
    for (int k = 1; k < length; ++k) {
        if ((s[k] & 0xc0) != 0x80) { *text += k; return '?'; }
        c = c << 6 | (s[k] & 0x3f);
    }
    *text += length;
    return c;
}

static const struct hud_glyph *glyph_of(const struct hud_font *font, uint32_t codepoint)
{
    for (int i = 0; i < font->count; ++i)
        if (font->glyphs[i].codepoint == codepoint) return &font->glyphs[i];
    return codepoint == '?' ? NULL : glyph_of(font, '?');
}

int hud_text(uint32_t *overlay, int x, int y, enum hud_face face, const char *text, uint32_t rgba)
{
    const struct hud_font *font = font_of(face);
    const int start = x;
    struct hud_rect box = {HUD_W, HUD_H, 0, 0};
    while (*text) {
        const struct hud_glyph *g = glyph_of(font, next_codepoint(&text));
        if (!g) continue;
        if (g->w && g->h) {
            if (x + g->xoff < box.x0) box.x0 = x + g->xoff;
            if (y + g->yoff < box.y0) box.y0 = y + g->yoff;
            if (x + g->xoff + g->w > box.x1) box.x1 = x + g->xoff + g->w;
            if (y + g->yoff + g->h > box.y1) box.y1 = y + g->yoff + g->h;
        }
        for (int j = 0; j < g->h; ++j) {
            const int py = y + g->yoff + j;
            if (py < 0 || py >= HUD_H) continue;
            for (int i = 0; i < g->w; ++i) {
                const int px = x + g->xoff + i;
                const uint32_t coverage = hud_atlas[(g->y + j) * HUD_ATLAS_W + g->x + i];
                if (px >= 0 && px < HUD_W && coverage) blend_color(&overlay[py * HUD_W + px], rgba, coverage);
            }
        }
        x += g->advance;
    }
    touch(box.x0, box.y0, box.x1, box.y1);
    return x - start;
}

int hud_text_width(enum hud_face face, const char *text)
{
    const struct hud_font *font = font_of(face);
    int width = 0;
    while (*text) {
        const struct hud_glyph *g = glyph_of(font, next_codepoint(&text));
        if (g) width += g->advance;
    }
    return width;
}

int hud_line_height(enum hud_face face)
{
    return font_of(face)->line_height;
}
