# Copyright (C) 2026 BlackBearReloaded
# SPDX-License-Identifier: GPL-3.0-or-later
"""Without border clear passes, every border a network pass reads is still zero.

Simulates the scratch cells around each tensor over several frames: the runtime zeroes
scratch before the first frame, the prepass writes the H cells of the output pixels, the
generated kernels write inside the current extent and zero the one-cell border around their
output, and the 3x3 layers read that border. Tensors of different levels share scratch
regions, so without the kernels' own border writes the borders would not stay zero. Both
resolution bands' layouts are checked.
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from build_fsr4_runtime import BORDER_CLEARS, NETWORK_PASSES  # noqa: E402
from fsr4_int8_kernels import LAYOUTS, PASSES, PLANES  # noqa: E402

SHIFT = {"H": 1, "Q": 2, "E": 3}
SIZES = {0: ((1920, 1080), (1280, 720), (1918, 1078), (321, 181), (16, 16)),
         1: ((3840, 2160), (2560, 1440), (2558, 1438), (1921, 1081))}


def extent(size, level):
    return tuple(((n + 7) & ~7) >> SHIFT[level] for n in size)


def address(layout, tensor, plane, x, y):
    base, level = tensor
    width, height = layout["levels"][level]
    return layout["bases"][base] // 16 + plane * width * height + (y + 1) * width + x + 1


def inside(layout, cell, tensor, x_end, y_end):
    """Whether a scratch cell lies in [0, x_end) x [0, y_end) of any plane of the tensor."""
    base, level = tensor
    width, height = layout["levels"][level]
    offset = cell - layout["bases"][base] // 16
    if not 0 <= offset < PLANES[level] * width * height:
        return False
    y, x = divmod(offset % (width * height), width)
    return 0 <= x - 1 < x_end and 0 <= y - 1 < y_end


def ring(layout, tensor, size, planes=None):
    """The one-cell border around the current extent."""
    w, h = extent(size, tensor[1])
    cells = [(x, y) for y in (-1, h) for x in range(-1, w + 1)] + [(x, y) for x in (-1, w) for y in range(h)]
    return {address(layout, tensor, plane, x, y) for plane in range(planes or PLANES[tensor[1]]) for x, y in cells}


def outputs():
    return {1: ("R0", "H")} | {i: PASSES[i]["output"] for i in NETWORK_PASSES}


def dirty_reads(layout, size, own_borders=True, frames=3):
    """(frame, dispatch) of every 3x3 layer that would read a written border cell."""
    tensors = outputs()
    watched = set()
    for tensor in set(tensors.values()):
        watched |= ring(layout, tensor, size)
    written, reads = {}, {}
    for dispatch, tensor in tensors.items():
        if dispatch == 1:  # the prepass writes the H cells of the output pixels
            x_end, y_end = ((n + 1) // 2 for n in size)
        else:
            x_end, y_end = extent(size, tensor[1])
            spec = PASSES[dispatch]
            if spec["kind"] != "down2x2":
                reads[dispatch] = ring(layout, spec["input"], size, 2 if spec["channels"] == 64 else 1)
        written[dispatch] = {cell for cell in watched if inside(layout, cell, tensor, x_end, y_end)}
    dirty = set()  # the runtime fills scratch with zeros before the first frame
    found = []
    for frame in range(frames):
        for dispatch in sorted(tensors):
            if reads.get(dispatch, set()) & dirty:
                found.append((frame, dispatch))
            dirty |= written[dispatch]
            if own_borders and dispatch != 1:
                dirty -= ring(layout, tensors[dispatch], size)
    return found


def clobbered(layout, size):
    """(dispatch, writer) where a kernel's border writes land inside a tensor still to be read."""
    tensors = outputs()
    last_writer, live_until = {}, {}
    for dispatch in sorted(tensors):
        if dispatch != 1:
            spec = PASSES[dispatch]
            for tensor in [spec["input"]] + ([spec["skip"]] if "skip" in spec else []):
                live_until[last_writer[tensor]] = dispatch
        last_writer[tensors[dispatch]] = dispatch
    live_until[last_writer[("R0", "H")]] = NETWORK_PASSES[-1] + 2  # the postpass reads it
    found = []
    for dispatch in NETWORK_PASSES:
        cells = ring(layout, tensors[dispatch], size)
        for writer, until in live_until.items():
            if writer < dispatch < until:
                tensor = tensors[writer]
                x_end, y_end = extent(size, tensor[1])
                if any(inside(layout, cell, tensor, x_end, y_end) for cell in cells):
                    found.append((dispatch, writer))
    return found


class ScratchBorders(unittest.TestCase):
    def test_border_writes_spare_live_tensors(self):
        for band, sizes in SIZES.items():
            for size in sizes:
                self.assertEqual(clobbered(LAYOUTS[band], size), [], (band, size))

    def test_kernels_keep_every_read_border_zero(self):
        self.assertEqual(BORDER_CLEARS, tuple(range(2, 27, 2)))
        for band, sizes in SIZES.items():
            for size in sizes:
                self.assertEqual(dirty_reads(LAYOUTS[band], size), [], (band, size))

    def test_shared_regions_need_the_border_writes(self):
        for band, sizes in SIZES.items():
            self.assertTrue(dirty_reads(LAYOUTS[band], sizes[0], own_borders=False), band)

    def test_layouts_fit_their_allocation(self):
        for band, layout in LAYOUTS.items():
            ends = [layout["bases"][base] + PLANES[level] * 16 * width * height
                    for base in layout["bases"] for level, (width, height) in layout["levels"].items()
                    if (base, level) in set(outputs().values())]
            self.assertEqual(max(ends), layout["scratch"], band)


if __name__ == "__main__":
    unittest.main()
