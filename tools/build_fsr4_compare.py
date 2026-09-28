#!/usr/bin/env python3
# Copyright (C) 2026 BlackBearReloaded
# SPDX-License-Identifier: GPL-3.0-or-later
"""Build the headless FSR4 comparison capture (PPSA88900) against the staged ps5_fsr4 SDK.

It renders fixed shots of the demo scene and writes, per shot, the render-resolution
frame, its bilinear upscale, the converged FSR4 output, a native output-resolution
render and a supersampled reference, plus a short orbiting clip; see
examples/fsr4_compare_main.c. tools/build_fsr4_comparisons.py turns the fetched files
into comparison images. Stage the SDK first with make driver-sdk and
tools/build_fsr4_sdk.py.
"""
import argparse
import json
from pathlib import Path
import struct
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from fsr4_paths import DIST_SDK  # noqa: E402
from build_fsr4_clear import build_native_app  # noqa: E402
from build_fsr4_demo import glslang  # noqa: E402

# The compare capture reuses the demo's scene and adds its own accumulation and export passes.
SHADERS = {"scene": "fsr4_demo_scene.comp", "accumulate": "fsr4_compare_accumulate.comp",
           "export": "fsr4_compare_export.comp"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pipeline-cache", type=Path, default=DIST_SDK / "share/ps5fsr4/pipeline-cache-ps5.bin",
                        help="Pipeline cache data from an earlier run on this driver")
    parser.add_argument("--out", type=Path, default=ROOT / "build/fsr4-compare")
    args = parser.parse_args()
    library = DIST_SDK / "lib/libps5_fsr4.a"
    if not library.is_file():
        raise SystemExit(f"{library} is missing; run tools/build_fsr4_sdk.py")
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    header = ["#include <stdint.h>"]
    for name, source in SHADERS.items():
        spv = out / f"fsr4_compare_{name}.spv"
        subprocess.run([glslang(), "-V", "--target-env", "vulkan1.1", str(ROOT / "examples" / source), "-o", str(spv)],
                       check=True, stdout=subprocess.DEVNULL)
        words = struct.unpack(f"<{spv.stat().st_size // 4}I", spv.read_bytes())
        header.append(f"static const uint32_t fsr4_compare_{name}_spv[] = {{" + ",".join(map(hex, words)) + "};")
    (out / "fsr4_compare_shaders.h").write_text("\n".join(header) + "\n")
    assets = out / "PPSA88900/assets"
    assets.mkdir(parents=True, exist_ok=True)
    cache = assets / "pipeline-cache.bin"
    if args.pipeline_cache and args.pipeline_cache.is_file():
        cache.write_bytes(args.pipeline_cache.read_bytes())
    elif cache.exists():
        cache.unlink()
    build_native_app(out, ROOT / "examples/fsr4_compare_main.c", "FSR4 Comparison Capture", libraries=[library])
    print(json.dumps(dict(package=str(out / "PPSA88900"), pipeline_cache=cache.exists()), indent=2))


if __name__ == "__main__":
    main()
