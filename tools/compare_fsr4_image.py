#!/usr/bin/env python3
"""Measure raw RGBA32F FSR4 output differences; never infer an acceptance threshold."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import struct


def compare(actual, reference, width, height):
    if width <= 0 or height <= 0 or len(actual) != width * height * 16 or len(reference) != len(actual):
        raise ValueError("Expected two complete width x height RGBA32F images")
    a = struct.unpack("<%df" % (len(actual) // 4), actual)
    b = struct.unpack("<%df" % (len(reference) // 4), reference)
    if not all(math.isfinite(v) for v in (*a, *b)):
        raise ValueError("Nonfinite image component")
    rgb = [abs(x-y) for i, (x, y) in enumerate(zip(a, b)) if i % 4 != 3]
    rmse = math.sqrt(math.fsum(e*e for e in rgb) / len(rgb))
    return dict(width=width, height=height, bytes=len(actual),
                actual_sha256=hashlib.sha256(actual).hexdigest(),
                reference_sha256=hashlib.sha256(reference).hexdigest(),
                byte_exact=actual == reference, acceptance_defined=False,
                differing_components=sum(x != y for x, y in zip(a, b)),
                rgb_max_absolute_error=max(rgb), rgb_rmse=rmse,
                rgb_psnr_unit_range_db=-20*math.log10(rmse) if rmse else None,
                alpha_max_absolute_error=max(abs(x-y) for x, y in zip(a[3::4], b[3::4])),
                actual_range=[min(a), max(a)], reference_range=[min(b), max(b)])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("actual", type=Path)
    parser.add_argument("reference", type=Path)
    parser.add_argument("--width", type=int, required=True)
    parser.add_argument("--height", type=int, required=True)
    args = parser.parse_args()
    print(json.dumps(compare(args.actual.read_bytes(), args.reference.read_bytes(),
                             args.width, args.height), indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
