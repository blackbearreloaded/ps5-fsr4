#!/usr/bin/env python3
# Copyright (C) 2026 BlackBearReloaded
# SPDX-License-Identifier: GPL-3.0-or-later
"""Build before/after comparison images from a FSR4 comparison capture.

The capture (examples/fsr4_compare_main.c, built by tools/build_fsr4_compare.py) writes
tonemapped BGRA8 frames of the demo scene. For each shot this writes:

  <shot>/full/<mode>.png   every capture at full size
  <shot>/crop_<mode>.png   1:1 crop of the most detailed window
  <shot>/zoom_<mode>.png   3x nearest-neighbour zoom of its busiest part
  <shot>/strip.png         the crops side by side, labelled
  <shot>/zoom.png          the zooms side by side, labelled
  <shot>/metrics.md        PSNR and SSIM of each 1080p image against the reference

and for the orbiting clip clip/clip.mp4 and clip/clip_zoom.mp4 (bilinear | FSR4 | native),
plus hero.png (a 2x2 sheet of the shot where FSR4 gains most) and an index README.md.
Metrics compare against the 64-sample supersampled render; SSIM is computed on luma with
a 7x7 uniform window.
"""
import argparse
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

OUTPUT = (1920, 1080)
CROP = (480, 270)
ZOOM_WINDOW, ZOOM = (240, 135), 3
FONT = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")


def load_log(capture):
    log = (capture / "fsr4-compare-log.txt").read_text(errors="replace")
    sizes = {m.group(1): (int(m.group(2)), int(m.group(3)))
             for m in re.finditer(r"FSR4_COMPARE_IMAGE name=(\S+) width=(\d+) height=(\d+)", log)}
    if "FSR4_COMPARE_END result=COMPLETE" not in log:
        raise SystemExit(f"{capture}: the capture did not complete")
    return sizes


def read(capture, sizes, name):
    w, h = sizes[name]
    data = np.fromfile(capture / f"fsr4-compare-{name}.bgra", np.uint8)
    if data.size != w * h * 4:
        raise SystemExit(f"{name}: expected {w}x{h}")
    return data.reshape(h, w, 4)[:, :, 2::-1].copy()


def luma(rgb):
    return rgb.astype(np.float64) @ np.array([0.299, 0.587, 0.114])


def box_mean(img, radius):
    n = 2 * radius + 1
    c = np.pad(img, ((1, 0), (1, 0))).cumsum(0).cumsum(1)
    return (c[n:, n:] - c[:-n, n:] - c[n:, :-n] + c[:-n, :-n]) / (n * n)


def ssim(a, b, radius=3):
    x, y = luma(a), luma(b)
    c1, c2 = (0.01 * 255) ** 2, (0.03 * 255) ** 2
    mx, my = box_mean(x, radius), box_mean(y, radius)
    sxx = box_mean(x * x, radius) - mx * mx
    syy = box_mean(y * y, radius) - my * my
    sxy = box_mean(x * y, radius) - mx * my
    return float((((2 * mx * my + c1) * (2 * sxy + c2)) / ((mx * mx + my * my + c1) * (sxx + syy + c2))).mean())


def psnr(a, b):
    mse = np.mean((a.astype(np.float64) - b.astype(np.float64)) ** 2)
    return float("inf") if mse == 0 else float(10 * np.log10(255.0 ** 2 / mse))


def busiest(reference, size, step=30):
    """Top-left corner of the window of `size` with the most gradient energy in the reference."""
    y = luma(reference)
    energy = np.abs(np.diff(y, axis=0))[:, :-1] + np.abs(np.diff(y, axis=1))[:-1, :]
    c = np.pad(energy, ((1, 0), (1, 0))).cumsum(0).cumsum(1)
    w, h = size
    best, corner = -1.0, (0, 0)
    for top in range(0, energy.shape[0] - h, step):
        for left in range(0, energy.shape[1] - w, step):
            total = c[top + h, left + w] - c[top, left + w] - c[top + h, left] + c[top, left]
            if total > best:
                best, corner = total, (left, top)
    return corner


def font(size):
    return ImageFont.truetype(str(FONT), size) if FONT.is_file() else ImageFont.load_default()


def labelled(images, labels, gap=6, bar=30):
    """Images side by side with a caption bar above each."""
    width = sum(i.width for i in images) + gap * (len(images) - 1)
    height = max(i.height for i in images) + bar
    sheet = Image.new("RGB", (width, height), (24, 24, 24))
    draw, face, x = ImageDraw.Draw(sheet), font(18), 0
    for image, label in zip(images, labels):
        sheet.paste(image, (x, bar))
        draw.text((x + 8, 5), label, fill=(240, 240, 240), font=face)
        x += image.width + gap
    return sheet


def build_shot(capture, sizes, shot, out):
    names = {"reference": f"{shot}-reference", "native": f"{shot}-native"}
    renders = sorted({m.group(1) for n in sizes for m in [re.match(rf"{shot}-(\d+x\d+)-fsr4$", n)] if m},
                     key=lambda r: -int(r.split("x")[0]))
    for r in renders:
        names[f"{r}-bilinear"] = f"{shot}-{r}-bilinear"
        names[f"{r}-fsr4"] = f"{shot}-{r}-fsr4"
    images = {mode: read(capture, sizes, name) for mode, name in names.items()}
    inputs = {r: read(capture, sizes, f"{shot}-{r}-input") for r in renders}
    full = out / shot / "full"
    full.mkdir(parents=True, exist_ok=True)
    for mode, rgb in images.items():
        Image.fromarray(rgb).save(full / f"{mode}.png")
    for r, rgb in inputs.items():
        Image.fromarray(rgb).save(full / f"{r}-input.png")

    reference = images["reference"]
    left, top = busiest(reference, CROP)
    zl, zt = busiest(reference[top:top + CROP[1], left:left + CROP[0]], ZOOM_WINDOW, step=15)
    zl, zt = left + zl, top + zt
    order, captions = [], []
    for r in renders:
        order += [f"{r}-bilinear", f"{r}-fsr4"]
        captions += [f"Bilinear from {r.replace('x', '×')}", f"FSR4 from {r.replace('x', '×')}"]
    order += ["native", "reference"]
    captions += ["Native 1080p (no AA)", "Reference (64× SSAA)"]
    crops, zooms = [], []
    for mode in order:
        crop = Image.fromarray(images[mode][top:top + CROP[1], left:left + CROP[0]])
        zoom = Image.fromarray(images[mode][zt:zt + ZOOM_WINDOW[1], zl:zl + ZOOM_WINDOW[0]]).resize(
            (ZOOM_WINDOW[0] * ZOOM, ZOOM_WINDOW[1] * ZOOM), Image.NEAREST)
        crop.save(out / shot / f"crop_{mode}.png")
        zoom.save(out / shot / f"zoom_{mode}.png")
        crops.append(crop)
        zooms.append(zoom)
    labelled(crops, captions).save(out / shot / "strip.png")
    per_row = 2 + (len(order) > 4)
    rows = [labelled(zooms[i:i + per_row], captions[i:i + per_row]) for i in range(0, len(zooms), per_row)]
    sheet = Image.new("RGB", (max(r.width for r in rows), sum(r.height for r in rows)), (24, 24, 24))
    y = 0
    for row in rows:
        sheet.paste(row, (0, y))
        y += row.height
    sheet.save(out / shot / "zoom.png")

    lines = [f"# {shot}", "", f"Crop window {CROP[0]}×{CROP[1]} at ({left}, {top}); zoom window "
             f"{ZOOM_WINDOW[0]}×{ZOOM_WINDOW[1]} at ({zl}, {zt}), shown {ZOOM}× nearest-neighbour.", "",
             "| Image | PSNR full (dB) | SSIM full | PSNR crop (dB) | SSIM crop |", "| --- | ---: | ---: | ---: | ---: |"]
    metrics = {}
    for mode, caption in zip(order[:-1], captions[:-1]):
        a = images[mode]
        ca, cr = a[top:top + CROP[1], left:left + CROP[0]], reference[top:top + CROP[1], left:left + CROP[0]]
        metrics[mode] = (psnr(a, reference), ssim(a, reference), psnr(ca, cr), ssim(ca, cr))
        lines.append(f"| {caption} | {metrics[mode][0]:.2f} | {metrics[mode][1]:.4f} | {metrics[mode][2]:.2f} | "
                     f"{metrics[mode][3]:.4f} |")
    (out / shot / "metrics.md").write_text("\n".join(lines) + "\n")
    return dict(renders=renders, order=order, captions=captions, metrics=metrics, zooms=dict(zip(order, zooms)))


def build_hero(shot, result, frame_ms, out):
    """A 2x2 sheet for the project README: bilinear and FSR4 from the lowest render size, then
    native 1080p and the reference, each 3x zoomed and captioned with its PSNR."""
    r = result["renders"][-1]
    cells = [(f"{r}-bilinear", f"Bilinear {r.replace('x', '×')} → 1080p"),
             (f"{r}-fsr4", f"FSR4 {r.replace('x', '×')} → 1080p"),
             ("native", "Native 1080p, no AA"), ("reference", "Reference, 64× supersampled")]
    captions = []
    for mode, caption in cells:
        if mode in result["metrics"]:
            caption += f"  ({result['metrics'][mode][0]:.1f} dB)"
        captions.append(caption)
    rows = [labelled([result["zooms"][cells[i][0]], result["zooms"][cells[i + 1][0]]], captions[i:i + 2], bar=34)
            for i in (0, 2)]
    title = f"FSR4 on PS5: {r.replace('x', '×')} → 1920×1080 in {frame_ms} ms  ·  {shot}, {ZOOM}× zoom"
    sheet = Image.new("RGB", (rows[0].width, rows[0].height * 2 + 44), (24, 24, 24))
    ImageDraw.Draw(sheet).text((8, 8), title, fill=(255, 255, 255), font=font(24))
    sheet.paste(rows[0], (0, 44))
    sheet.paste(rows[1], (0, 44 + rows[0].height))
    sheet.save(out / "hero.png")


def build_clip(capture, sizes, out):
    frames = sorted(int(m.group(1)) for n in sizes for m in [re.match(r"clip-fsr4-(\d+)$", n)] if m)
    if not frames or not shutil.which("ffmpeg"):
        return None
    clip = out / "clip"
    clip.mkdir(parents=True, exist_ok=True)
    modes, captions = ("bilinear", "fsr4", "native"), ("Bilinear from 1280×720", "FSR4 from 1280×720",
                                                       "Native 1080p (no AA)")
    with tempfile.TemporaryDirectory() as tmp:
        for n in frames:
            panels = [Image.fromarray(read(capture, sizes, f"clip-{m}-{n:03d}")) for m in modes]
            labelled(panels, captions).save(Path(tmp) / f"full{n:03d}.png")
            w, h = panels[0].size
            box = (w // 4, h // 4, w // 4 + w // 2, h // 4 + h // 2)
            zoomed = [p.crop(box).resize((w, h), Image.NEAREST) for p in panels]
            labelled(zoomed, [c + " (2× zoom)" for c in captions]).save(Path(tmp) / f"zoom{n:03d}.png")
        for prefix, name in (("full", "clip.mp4"), ("zoom", "clip_zoom.mp4")):
            subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-framerate", "60", "-i",
                            str(Path(tmp) / f"{prefix}%03d.png"), "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2",
                            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "12", str(clip / name)], check=True)
        shutil.copy(Path(tmp) / f"zoom{frames[len(frames) // 2]:03d}.png", clip / "clip_zoom_still.png")
    return len(frames)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", type=Path, help="Directory with the fetched fsr4-compare-* files")
    parser.add_argument("--out", type=Path, default=Path(__file__).resolve().parents[1] / "build/fsr4-comparisons")
    parser.add_argument("--hero", help="Shot for hero.png (default: the largest FSR4 gain at the lowest render size)")
    parser.add_argument("--frame-ms", default="2.9", help="FSR4 frame time quoted in hero.png")
    args = parser.parse_args()
    capture, out = args.capture.resolve(), args.out.resolve()
    sizes = load_log(capture)
    shots = sorted({m.group(1) for n in sizes for m in [re.match(r"(.+)-reference$", n)] if m})
    out.mkdir(parents=True, exist_ok=True)
    index = ["# FSR4 on PS5: before and after", "",
             "Frames of the demo scene (`examples/fsr4_demo_scene.comp`) captured on the console by "
             "`examples/fsr4_compare_main.c`: a bilinear upscale of the render, the FSR4 output after the "
             "static shot converged (48 jittered frames), a native 1080p render without anti-aliasing and a "
             "64-sample supersampled reference. All are tonemapped like the demo. Metrics are against the "
             "reference (SSIM on luma, 7×7 window).", "",
             "> [!CAUTION]", "> GitHub scales and compresses images shown inside a page. For the real pixels, open "
             "a file such as `overview/full/960x540-fsr4.png` and use Raw or Download.", ""]
    results = {}
    for shot in shots:
        result = results[shot] = build_shot(capture, sizes, shot, out)
        index += [f"## {shot}", "", f"![{shot} zoom]({shot}/zoom.png)", "", f"![{shot} crops]({shot}/strip.png)", "",
                  "| Image | PSNR (dB) | SSIM |", "| --- | ---: | ---: |"]
        for mode, caption in zip(result["order"][:-1], result["captions"][:-1]):
            p, s = result["metrics"][mode][:2]
            index.append(f"| {caption} | {p:.2f} | {s:.4f} |")
        index += ["", f"Full frames: [{shot}/full/]({shot}/full/). Crops, zooms and windows: "
                  f"[{shot}/metrics.md]({shot}/metrics.md).", ""]

    def gain(shot):
        r = results[shot]["renders"][-1]
        return results[shot]["metrics"][f"{r}-fsr4"][0] - results[shot]["metrics"][f"{r}-bilinear"][0]

    hero = args.hero or max(results, key=gain)
    build_hero(hero, results[hero], args.frame_ms, out)
    index[2:2] = ["![FSR4 before and after](hero.png)", ""]
    frames = build_clip(capture, sizes, out)
    if frames:
        index += ["## Motion", "", f"{frames} frames of an orbiting camera with the rotor spinning: "
                  "[clip.mp4](clip/clip.mp4), [2× zoom](clip/clip_zoom.mp4).", "",
                  "![clip still](clip/clip_zoom_still.png)", ""]
    (out / "README.md").write_text("\n".join(index))
    print(out / "README.md")


if __name__ == "__main__":
    main()
