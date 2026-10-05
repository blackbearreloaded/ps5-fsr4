/* Copyright (C) 2026 BlackBearReloaded
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * PS5 FSR4 Showcase HUD: text and panels drawn by the CPU into a 1920x1080
 * premultiplied RGBA8 overlay that the compose shader lays over the frame.
 * Colors are packed as r | g << 8 | b << 16 | a << 24, the layout of
 * unpackUnorm4x8. Drawing records damage rectangles so that a redraw clears
 * and uploads only what the previous and the new HUD cover.
 */
#ifndef FSR4_SHOWCASE_HUD_H
#define FSR4_SHOWCASE_HUD_H

#include <stdint.h>

enum { HUD_W = 1920, HUD_H = 1080 };
enum hud_face { HUD_TITLE, HUD_BODY, HUD_SMALL };
struct hud_rect { int x0, y0, x1, y1; };  /* [x0, x1) x [y0, y1) */

#define HUD_RGBA(r, g, b, a) ((uint32_t)(r) | (uint32_t)(g) << 8 | (uint32_t)(b) << 16 | (uint32_t)(a) << 24)

/* Starts a redraw: clears what the previous one drew. */
void hud_begin(uint32_t *overlay);
/* The rectangles the previous and the current redraw touched; returns their count. */
int hud_damage(struct hud_rect *rects, int max);
/* Blends a rectangle of straight-alpha color with rounded corners. */
void hud_fill(uint32_t *overlay, int x, int y, int w, int h, int radius, uint32_t rgba);
/* Draws UTF-8 text with its top-left corner at (x, y); returns the advance in pixels. */
int hud_text(uint32_t *overlay, int x, int y, enum hud_face face, const char *text, uint32_t rgba);
int hud_text_width(enum hud_face face, const char *text);
int hud_line_height(enum hud_face face);

#endif
