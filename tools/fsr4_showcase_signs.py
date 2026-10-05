#!/usr/bin/env python3
# Copyright (C) 2026 BlackBearReloaded
# SPDX-License-Identifier: GPL-3.0-or-later
"""The showcase city's signs: four 2048x1024 signs of 8-bit coverage in one 4096x2048 image.

apps/showcase/city.comp reads it from a storage buffer and thresholds it, so
the lettering keeps a hard edge at any magnification. The text is rasterized
from DejaVu Sans (fonts-dejavu-core) with Pillow.

usage: fsr4_showcase_signs.py OUT.bin [PREVIEW.png]
"""
from pathlib import Path
import sys

TILE_W, TILE_H = 2048, 1024
FONT_DIRS = (Path("/usr/share/fonts/truetype/dejavu"), Path("/usr/share/fonts/dejavu"))
READING_ROWS = (("F S R", 210), ("U P S C A L E", 150), ("T E M P O R A L", 112), ("R E C O N S T R U C T", 84),
                ("S U B P I X E L   D E T A I L", 64), ("E V E R Y   F R A M E   A D D S   S A M P L E S", 48),
                ("T H I N   L I N E S   S T A Y   W H O L E", 36),
                ("W I R E S   F E N C E S   L O U V R E S   B R I C K   T E X T", 27),
                ("T H E   N E T W O R K   R U N S   O N   T H E   C O N S O L E   G P U", 20),
                ("I F   Y O U   C A N   R E A D   T H I S   T H E   U P S C A L E R   I S   D O I N G   I T S   J O B",
                 15))
NEON = ("HOTEL AURORA", "RAMEN · OPEN 24H", "ARCADE · KARAOKE", "NIGHT MARKET")
SMALL_PRINT = (
    "The city is drawn at a fraction of the size you are looking at: as little as one pixel in nine. "
    "Every frame the camera is shifted by less than a pixel, so each frame sees the scene from a slightly "
    "different place. FSR 4 follows every pixel through time with the motion vectors and the depth the "
    "scene writes, and a neural network decides, pixel by pixel, how much of the past to keep. "
    "What comes out has the detail of many frames: wires thinner than a pixel stay whole, brick and "
    "louvres stop shimmering, and this text becomes readable. The network is the FSR 4.1.1 model, "
    "evaluated with 8-bit integer arithmetic by compute shaders written for the console GPU. "
    "At 1920x1080 it takes under three milliseconds; at 3840x2160, under eleven. "
    "Open the settings to change the render and output sizes, to split the screen against a plain "
    "bilinear upscale of the same frame or against a native render, to magnify a part of the picture, "
    "and to measure the upscaler on your own console.")


def font_path(name):
    for folder in FONT_DIRS:
        if (folder / name).is_file():
            return folder / name
    raise SystemExit(f"{name} is missing; install fonts-dejavu-core")


def build_signs():
    """The atlas as a Pillow image in mode L: title, reading chart, neon signs, small print."""
    from PIL import Image, ImageDraw, ImageFont

    def face(size, bold=True):
        return ImageFont.truetype(str(font_path("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf")), size)

    def centred(draw, y, text, font):
        draw.text(((TILE_W - draw.textlength(text, font=font)) / 2, y), text, font=font, fill=255)

    tiles = []
    image = Image.new("L", (TILE_W, TILE_H))
    draw = ImageDraw.Draw(image)
    centred(draw, 60, "FSR 4", face(520))
    centred(draw, 680, "on PlayStation 5", face(150))
    centred(draw, 890, "machine-learning upscaling · ps5-fsr4", face(64, False))
    tiles.append(image)

    image = Image.new("L", (TILE_W, TILE_H))
    draw = ImageDraw.Draw(image)
    y = 24
    for n, (text, size) in enumerate(READING_ROWS):
        centred(draw, y, text, face(size))
        draw.text((36, y + size * 0.25), str(n + 1), font=face(max(size // 2, 14), False), fill=255)
        y += int(size * 1.22) + 6
    tiles.append(image)

    image = Image.new("L", (TILE_W, TILE_H))
    draw = ImageDraw.Draw(image)
    for n, text in enumerate(NEON):
        centred(draw, n * 256 + 34, text, face(170))
        draw.rectangle((60, n * 256 + 236, TILE_W - 60, n * 256 + 244), fill=255)
    tiles.append(image)

    image = Image.new("L", (TILE_W, TILE_H))
    draw = ImageDraw.Draw(image)
    centred(draw, 30, "HOW THIS PICTURE IS MADE", face(92))
    font, margin, y, line = face(44, False), 70, 170, ""
    for word in SMALL_PRINT.split():
        if draw.textlength(line + " " + word, font=font) > TILE_W - 2 * margin:
            draw.text((margin, y), line, font=font, fill=255)
            line, y = word, y + 58
        else:
            line = (line + " " + word).strip()
    draw.text((margin, y), line, font=font, fill=255)
    tiles.append(image)

    atlas = Image.new("L", (2 * TILE_W, 2 * TILE_H))
    for n, tile in enumerate(tiles):
        atlas.paste(tile, ((n & 1) * TILE_W, (n >> 1) * TILE_H))
    return atlas


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    signs = build_signs()
    Path(sys.argv[1]).write_bytes(signs.tobytes())
    if len(sys.argv) > 2:
        signs.save(sys.argv[2])
