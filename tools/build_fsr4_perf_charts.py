#!/usr/bin/env python3
# Copyright (C) 2026 BlackBearReloaded
# SPDX-License-Identifier: GPL-3.0-or-later
"""Draw the README's FSR4 performance charts as SVG, in light and dark variants.

The numbers are PS5 measurements recorded below: the per-case times come from the headless
benchmark (tools/build_fsr4_bench.py), the optimization steps from 1280x720 -> 1920x1080 runs
timed from submission to completion. Update them here and rerun after a new measurement.
"""
import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# (output, render, mode, milliseconds per frame in the headless benchmark)
CASES = [
    ("1920×1080", "640×360", "Ultra Performance", 2.92),
    ("1920×1080", "1280×720", "Quality", 2.95),
    ("2560×1440", "1280×720", "Performance", 4.92),
    ("2560×1440", "1706×960", "Quality", 4.95),
    ("3840×2160", "1920×1080", "Performance", 10.86),
    ("3840×2160", "1280×720", "Ultra Performance", 10.94),
]
FRAME_60FPS = 1000 / 60

# 1280x720 -> 1920x1080, milliseconds per frame after each step, as measured
HISTORY = [
    ("First native run", "27"),
    ("Fused FP32 multiply-add", "8.0"),
    ("Wave64 where it is faster", "7.07"),
    ("Shader code prefetch into L2", "6.27"),
    ("Forward instruction prefetch", "3.81"),
    ("Persistent command region", "3.46"),
    ("Generated INT8 network kernels", "3.32"),
    ("Looped kernels, cheaper requantization", "3.05"),
    ("INT8 head linked into the postpass", "2.90"),
    ("Hardware FP16 conversions, no border clears", "2.67"),
]
BC250_MS = 3.93

THEMES = {
    "light": dict(background="#ffffff", text="#1f2328", muted="#59636e", grid="#d1d9e0", reference="#cf222e", bar="#0969da",
                  best="#1a7f37", on_bar="#ffffff",
                  outputs={"1920×1080": "#0969da", "2560×1440": "#8250df", "3840×2160": "#bc4c00"}),
    "dark": dict(background="#0d1117", text="#f0f6fc", muted="#9198a1", grid="#3d444d", reference="#f85149", bar="#4493f8",
                 best="#3fb950", on_bar="#0d1117",
                 outputs={"1920×1080": "#4493f8", "2560×1440": "#ab7df8", "3840×2160": "#f0883e"}),
}
FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI','Noto Sans',Helvetica,Arial,sans-serif"


class Svg:
    def __init__(self, width, height, title):
        self.width, self.height, self.parts = width, height, []
        self.head = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
                     f'viewBox="0 0 {width} {height}" role="img" aria-label="{title}" font-family="{FONT}">'
                     f'<title>{title}</title>')

    def text(self, x, y, content, size=12, fill="#000", anchor="start", weight="normal", halo=None):
        """`halo` outlines the glyphs in the page colour so lines behind them stay out of the way."""
        outline = (f' stroke="{halo}" stroke-width="4" stroke-linejoin="round" paint-order="stroke"'
                   if halo else "")
        self.parts.append(f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" fill="{fill}" '
                          f'text-anchor="{anchor}" font-weight="{weight}"{outline}>{content}</text>')

    def line(self, x1, y1, x2, y2, stroke, width=1, dash=None):
        dashes = f' stroke-dasharray="{dash}"' if dash else ""
        self.parts.append(f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
                          f'stroke="{stroke}" stroke-width="{width}"{dashes}/>')

    def rect(self, x, y, w, h, fill):
        self.parts.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" rx="3" fill="{fill}"/>')

    def source(self):
        return self.head + "".join(self.parts) + "</svg>\n"


def axis(svg, theme, left, right, top, bottom, limit, step, unit="ms"):
    """Vertical grid lines every `step` from 0 to `limit`, labelled under the chart."""
    scale = (right - left) / limit
    value = 0
    while value <= limit + 1e-9:
        x = left + value * scale
        svg.line(x, top, x, bottom, theme["grid"])
        svg.text(x, bottom + 16, f"{value:g}" + (f" {unit}" if value + step > limit else ""), 11,
                 theme["muted"], "middle")
        value += step
    return scale


def cases_chart(theme):
    width, left, right, row, gap = 820, 262, 770, 40, 12
    groups = len({output for output, *_ in CASES})
    top = 96
    bottom = top + row * len(CASES) + gap * (groups - 1)
    svg = Svg(width, bottom + 34, "FSR4 on the PS5: milliseconds per frame for each output and mode")
    svg.text(16, 26, "FSR4 on the PS5: time per frame", 17, theme["text"], weight="600")
    svg.text(16, 46, "Headless benchmark, milliseconds per frame (lower is better)", 12, theme["muted"])
    x = 16
    for output, color in theme["outputs"].items():
        svg.rect(x, 60, 12, 12, color)
        svg.text(x + 18, 70.5, f"{output} output", 12, theme["text"])
        x += 150
    limit = 18
    scale = axis(svg, theme, left, right, top - 8, bottom, limit, 2)
    budget = left + FRAME_60FPS * scale
    svg.line(budget, top - 8, budget, bottom, theme["reference"], 1.5, "5 4")
    svg.text(budget - 6, top - 14, "one 60 fps frame: 16.7 ms", 11, theme["reference"], "end")
    y, previous = top, CASES[0][0]
    for output, render, mode, ms in CASES:
        if output != previous:
            y += gap
            previous = output
        svg.text(left - 12, y + 13, f"{render} → {output}", 13, theme["text"], "end", "600")
        svg.text(left - 12, y + 28, mode, 11, theme["muted"], "end")
        svg.rect(left, y + 4, ms * scale, 22, theme["outputs"][output])
        svg.text(left + ms * scale + 6, y + 19.5, f"{ms:.2f} ms", 12, theme["text"], weight="600",
                 halo=theme["background"])
        y += row
    return svg.source()


def history_chart(theme):
    width, left, right, row = 820, 318, 790, 30
    top = 76
    bottom = top + row * len(HISTORY)
    svg = Svg(width, bottom + 34, "FSR4 at 1280x720 to 1920x1080 on the PS5 after each optimization step")
    svg.text(16, 26, "1280×720 → 1920×1080: optimization steps", 17, theme["text"], weight="600")
    svg.text(16, 46, "Milliseconds per frame from submission to completion (lower is better)", 12, theme["muted"])
    limit = 28
    scale = axis(svg, theme, left, right, top - 8, bottom, limit, 4)
    reference = left + BC250_MS * scale
    svg.line(reference, top - 8, reference, bottom, theme["reference"], 1.5, "5 4")
    svg.text(reference + 6, top - 14, f"BC250 running AMD's shaders: {BC250_MS} ms", 11, theme["reference"])
    for index, (step, measured) in enumerate(HISTORY):
        y, ms = top + index * row, float(measured)
        last = index == len(HISTORY) - 1
        end = left + ms * scale
        svg.text(left - 12, y + 16, step, 12, theme["text"], "end", "600" if last else "normal")
        svg.rect(left, y + 3, ms * scale, 18, theme["best"] if last else theme["bar"])
        label = f"{measured} ms"
        if end + 64 > width:
            svg.text(end - 6, y + 16.5, label, 12, theme["on_bar"], "end", "600")
        else:
            svg.text(end + 6, y + 16.5, label, 12, theme["text"], weight="600" if last else "normal",
                     halo=theme["background"])
    return svg.source()


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=ROOT / "docs/perf")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    for name, chart in (("cases", cases_chart), ("history", history_chart)):
        for theme_name, theme in THEMES.items():
            path = args.out / f"{name}-{theme_name}.svg"
            path.write_text(chart(theme), encoding="utf-8")
            print(path)


if __name__ == "__main__":
    main()
