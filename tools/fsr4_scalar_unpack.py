#!/usr/bin/env python3
# Copyright (C) 2026 BlackBearReloaded
# SPDX-License-Identifier: GPL-3.0-or-later
"""Make packed i32 -> two i16 bitcasts explicit for the WARP reference.

The original shaders already execute this arithmetic correctly through SPIR-V.
This is an oracle workaround, not a change to the PS5 model or its weights.
"""
import argparse
from pathlib import Path
import re

CAST = re.compile(r"  (%[\w.]+) = bitcast i32 (%[\w.]+) to <2 x i16>")
PREFIX = "%fsr4unpack"


def lower(source):
    if PREFIX in source:
        raise ValueError("Reserved scalar-unpack name already present")
    lines, count = [], 0
    for line in source.splitlines():
        match = CAST.fullmatch(line)
        if not match:
            lines.append(line)
            continue
        dest, value = match.groups()
        stem = f"{PREFIX}{count}"
        count += 1
        lines.extend([
            f"  {stem}lo = trunc i32 {value} to i16",
            f"  {stem}hi32 = lshr i32 {value}, 16",
            f"  {stem}hi = trunc i32 {stem}hi32 to i16",
            f"  {stem}vec = insertelement <2 x i16> undef, i16 {stem}lo, i32 0",
            f"  {dest} = insertelement <2 x i16> {stem}vec, i16 {stem}hi, i32 1",
        ])
    return "\n".join(lines) + "\n", count


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    result, count = lower(args.source.read_text())
    with args.output.open("x") as stream:
        stream.write(result)
    print(f"Expanded {count} packed casts")
