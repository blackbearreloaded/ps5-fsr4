#!/usr/bin/env python3
# Copyright (C) 2026 BlackBearReloaded
# SPDX-License-Identifier: GPL-3.0-or-later
"""Compose the PS5 FSR4 Showcase's launch assets (examples/fsr4_showcase/sce_sys).

The pictures are stills of the app's own scene, rendered off-screen from
examples/fsr4_showcase/city.comp and averaged over jittered samples:

  --icon STILL   a square still       -> icon0.png, 512x512, with the title
  --pic0 STILL   a 3840x2160 still    -> pic0.dds, the launch screen, with the title
  --pic1 STILL   a 3840x2160 still    -> pic1.dds, the home screen background
  --music        synthesize the selection music -> snd0.wav (48 kHz stereo, loops)

The backgrounds are written as BC7 with a DX10 header, encoded here (mode 6
only: one pair of endpoints per block, which suits a still without alpha).
snd0.wav becomes snd0.at9 with https://github.com/blackbearreloaded/ps5-at9-converter.
Needs numpy and Pillow, and DejaVu Sans (fonts-dejavu-core) for the lettering.
"""
import argparse
from pathlib import Path
import struct
import sys
import wave

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from fsr4_showcase_signs import font_path  # noqa: E402

TITLE, TAGLINE = "PS5 FSR4 Showcase", "FSR 4 upscaling on the console GPU"
BC7_WEIGHTS = np.array([0, 4, 9, 13, 17, 21, 26, 30, 34, 38, 43, 47, 51, 55, 60, 64], np.float32) / 64.0


def bc7_blocks(rgb):
    """BC7 mode 6 blocks of an opaque image: uint8 (h, w, 3) with h and w multiples of 4."""
    h, w, _ = rgb.shape
    pixels = rgb.reshape(h // 4, 4, w // 4, 4, 3).transpose(0, 2, 1, 3, 4).reshape(-1, 16, 3)
    out = []
    for first in range(0, len(pixels), 1 << 15):
        block = pixels[first:first + (1 << 15)].astype(np.float32)
        mean = block.mean(1, keepdims=True)
        centred = block - mean
        # The line the block's colors lie along: the main axis of their covariance.
        covariance = np.einsum("bpi,bpj->bij", centred, centred)
        axis = block.max(1) - block.min(1) + 1e-3
        for _ in range(8):
            axis = np.einsum("bij,bj->bi", covariance, axis)
            axis /= np.linalg.norm(axis, axis=1, keepdims=True) + 1e-9
        flat = np.linalg.norm(axis, axis=1) < 0.5
        axis[flat] = (1.0, 0.0, 0.0)
        along = np.einsum("bpi,bi->bp", centred, axis)
        # Endpoints: 7 bits and a shared low bit per endpoint. The low bit is 1, so alpha is 255.
        q0 = np.clip(np.round((mean[:, 0] + axis * along.min(1, keepdims=True) - 1) / 2), 0, 127).astype(np.int64)
        q1 = np.clip(np.round((mean[:, 0] + axis * along.max(1, keepdims=True) - 1) / 2), 0, 127).astype(np.int64)
        e0, e1 = (2 * q0 + 1).astype(np.float32), (2 * q1 + 1).astype(np.float32)
        span = e1 - e0
        t = np.einsum("bpi,bi->bp", block - e0[:, None, :], span) / np.maximum((span * span).sum(1, keepdims=True), 1e-6)
        index = np.abs(t[:, :, None] - BC7_WEIGHTS[None, None, :]).argmin(2).astype(np.int64)
        swap = index[:, 0] >= 8                      # the first index is stored without its top bit
        index[swap] = 15 - index[swap]
        q0[swap], q1[swap] = q1[swap], q0[swap].copy()
        q0, q1, index = q0.astype(np.uint64), q1.astype(np.uint64), index.astype(np.uint64)
        low = np.full(len(block), 1 << 6, np.uint64)  # mode 6
        for n, value in enumerate((q0[:, 0], q1[:, 0], q0[:, 1], q1[:, 1], q0[:, 2], q1[:, 2])):
            low |= value << np.uint64(7 + 7 * n)
        low |= np.uint64(127 << 49) | np.uint64(127 << 56) | np.uint64(1 << 63)
        high = np.uint64(1) | index[:, 0] << np.uint64(1)
        for k in range(1, 16):
            high |= index[:, k] << np.uint64(4 * k)
        out.append(np.stack([low, high], 1).astype("<u8").tobytes())
    return b"".join(out)


def write_dds(path, image):
    """A 3840x2160 BC7 image with a DX10 header, as the console's sce_sys expects."""
    if image.size != (3840, 2160):
        raise SystemExit(f"{path.name} needs a 3840x2160 still, not {image.size[0]}x{image.size[1]}")
    data = bc7_blocks(np.asarray(image.convert("RGB")))
    header = struct.pack("<4s7I44x", b"DDS ", 124, 0x000A1007, 2160, 3840, len(data), 1, 1)
    header += struct.pack("<II4s20x", 32, 4, b"DX10") + struct.pack("<I16x", 0x1000)
    header += struct.pack("<5I", 98, 3, 0, 1, 1)     # DXGI_FORMAT_BC7_UNORM, a 2D texture, straight alpha
    path.write_bytes(header + data)


def lettering(image, title_size, tagline_size, margin, bottom):
    """The title and the tagline over a shade that keeps them readable."""
    from PIL import Image, ImageDraw, ImageFont
    w, h = image.size
    shade = Image.new("L", (1, h))
    start = bottom - title_size * 3.2
    shade.putdata([int(210 * min(1.0, max(0.0, (y - start) / (title_size * 2.4)))) for y in range(h)])
    image = Image.composite(Image.new("RGB", (w, h), (4, 6, 14)), image, shade.resize((w, h)))
    draw = ImageDraw.Draw(image)
    bold = ImageFont.truetype(str(font_path("DejaVuSans-Bold.ttf")), title_size)
    plain = ImageFont.truetype(str(font_path("DejaVuSans.ttf")), tagline_size)
    draw.text((margin, bottom - title_size - tagline_size * 2.2), TITLE, font=bold, fill=(255, 255, 255))
    draw.rectangle((margin, bottom - tagline_size * 1.55, margin + title_size * 1.4, bottom - tagline_size * 1.55 + max(3, title_size // 14)),
                   fill=(255, 132, 44))
    draw.text((margin, bottom - tagline_size * 1.15), TAGLINE, font=plain, fill=(200, 208, 220))
    return image


def music(seconds=32.0, rate=48000):
    """A slow loop for the home screen: four chords of soft pads, a plucked figure and a long room."""
    n = int(seconds * rate)
    t = np.arange(n) / rate
    random = np.random.default_rng(4)
    note = lambda semitones: 110.0 * 2.0 ** (semitones / 12.0)   # from A2
    chords = ((5, 12, 20, 27, 31), (1, 8, 17, 24, 29), (-4, 8, 15, 24, 31), (3, 10, 19, 24, 29))  # Dm9, Bbmaj7, Fmaj7, C6/9
    bar = seconds / len(chords)
    left, right = np.zeros(n), np.zeros(n)
    for c, chord in enumerate(chords):
        phase = (t - c * bar) % seconds                           # time since this chord began, around the loop
        swell = np.clip(phase / 2.5, 0, 1) * np.clip((bar + 2.5 - phase) / 2.5, 0, 1)
        swell = np.sin(swell * np.pi / 2) ** 2
        for k, semitones in enumerate(chord):
            for cents, pan in ((-4, 0.3), (5, 0.7)):
                f = round(note(semitones) * 2 ** (cents / 1200) * seconds) / seconds   # a whole number of cycles
                voice = sum(np.sin(2 * np.pi * f * h * t + random.uniform(0, 6.28)) / h ** 1.8 for h in range(1, 6))
                voice *= swell * (0.75 + 0.25 * np.sin(2 * np.pi * t * (1 + k) / seconds * 3)) / (1 + 0.25 * k)
                left += voice * (1 - pan)
                right += voice * pan
        for step in range(int(bar * 2)):                          # two plucks a second from the chord
            at = c * bar + step * 0.5
            semitones = chord[1 + (step * 3 + c) % 4] + 24
            since = (t - at) % seconds
            pluck = np.sin(2 * np.pi * note(semitones) * since) * np.exp(-since * 4.0) * (since < 3.0)
            pan = 0.25 + 0.5 * ((step * 5 + c) % 4) / 3
            left += pluck * (1 - pan) * 0.9
            right += pluck * pan * 0.9
    tail = np.arange(int(3.0 * rate)) / rate                      # the room: decaying noise, wrapped around the loop
    wet = []
    for channel in (left, right):
        room = np.zeros(n)
        room[:len(tail)] = random.standard_normal(len(tail)) * np.exp(-tail / 0.7)
        wet.append(np.fft.irfft(np.fft.rfft(channel) * np.fft.rfft(room / np.sqrt((room ** 2).sum())), n))
    stereo = np.stack([left + 0.5 * wet[0], right + 0.5 * wet[1]], 1)
    return stereo / np.abs(stereo).max() * 0.7


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--icon", type=Path)
    parser.add_argument("--pic0", type=Path)
    parser.add_argument("--pic1", type=Path)
    parser.add_argument("--music", action="store_true")
    parser.add_argument("--out", type=Path, default=ROOT / "examples/fsr4_showcase/sce_sys")
    args = parser.parse_args()
    from PIL import Image
    args.out.mkdir(parents=True, exist_ok=True)
    if args.icon:
        icon = Image.open(args.icon).convert("RGB").resize((512, 512), Image.LANCZOS)
        from PIL import ImageDraw, ImageFont
        shade = Image.new("L", (1, 512))
        shade.putdata([int(215 * min(1.0, max(0.0, (y - 300) / 130))) for y in range(512)])
        icon = Image.composite(Image.new("RGB", (512, 512), (4, 6, 14)), icon, shade.resize((512, 512)))
        draw = ImageDraw.Draw(icon)
        bold = ImageFont.truetype(str(font_path("DejaVuSans-Bold.ttf")), 104)
        small = ImageFont.truetype(str(font_path("DejaVuSans-Bold.ttf")), 30)
        draw.text(((512 - draw.textlength("FSR 4", font=bold)) / 2, 340), "FSR 4", font=bold, fill=(255, 255, 255))
        draw.rectangle((176, 456, 336, 461), fill=(255, 132, 44))
        spaced = " ".join("SHOWCASE")
        draw.text(((512 - draw.textlength(spaced, font=small)) / 2, 468), spaced, font=small, fill=(200, 208, 220))
        icon.save(args.out / "icon0.png", optimize=True)
    if args.pic0:
        write_dds(args.out / "pic0.dds", lettering(Image.open(args.pic0).convert("RGB"), 150, 58, 190, 1960))
    if args.pic1:
        write_dds(args.out / "pic1.dds", Image.open(args.pic1).convert("RGB"))
    if args.music:
        samples = (music() * 32767).astype("<i2")
        with wave.open(str(args.out / "snd0.wav"), "wb") as f:
            f.setnchannels(2)
            f.setsampwidth(2)
            f.setframerate(48000)
            f.writeframes(samples.tobytes())


if __name__ == "__main__":
    main()
