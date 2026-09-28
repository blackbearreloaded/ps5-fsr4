#!/usr/bin/env python3
# Copyright (C) 2026 BlackBearReloaded
# SPDX-License-Identifier: GPL-3.0-or-later
"""Build the interactive native FSR4 demo (PPSA88900) against the staged ps5_fsr4 SDK.

Stage the SDK first with make driver-sdk and tools/build_fsr4_sdk.py; the demo
uses only its public headers, archives and warmed pipeline cache.
"""
import argparse
import json
import os
from pathlib import Path
import struct
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from fsr4_paths import DIST_SDK, TOOLCHAIN_BIN  # noqa: E402
from build_fsr4_clear import build_native_app  # noqa: E402

SHADERS = ("scene.comp", "present.comp", "blit.vert", "blit.frag")


def glslang():
    local = TOOLCHAIN_BIN / "glslangValidator"
    return str(local) if local.exists() else os.environ.get("GLSLANG", "glslangValidator")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pipeline-cache", type=Path, default=DIST_SDK / "share/ps5fsr4/pipeline-cache-ps5.bin",
                        help="Pipeline cache data from an earlier run on this driver")
    parser.add_argument("--out", type=Path, default=ROOT / "build/fsr4-demo")
    args = parser.parse_args()
    library = DIST_SDK / "lib/libps5_fsr4.a"
    if not library.is_file():
        raise SystemExit(f"{library} is missing; run tools/build_fsr4_sdk.py")
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    header = ["#include <stdint.h>"]
    for shader in SHADERS:
        source = ROOT / f"examples/fsr4_demo_{shader}"
        name = shader.replace(".comp", "").replace(".", "_")
        spv = out / f"fsr4_demo_{name}.spv"
        subprocess.run([glslang(), "-V", "--target-env", "vulkan1.1", str(source), "-o", str(spv)],
                       check=True, stdout=subprocess.DEVNULL)
        words = struct.unpack(f"<{spv.stat().st_size // 4}I", spv.read_bytes())
        header.append(f"static const uint32_t fsr4_demo_{name}_spv[] = {{" + ",".join(map(hex, words)) + "};")
    (out / "fsr4_demo_shaders.h").write_text("\n".join(header) + "\n")
    assets = out / "PPSA88900/assets"
    assets.mkdir(parents=True, exist_ok=True)
    cache = assets / "pipeline-cache.bin"
    if args.pipeline_cache and args.pipeline_cache.is_file():
        cache.write_bytes(args.pipeline_cache.read_bytes())
    elif cache.exists():
        cache.unlink()
    build_native_app(out, ROOT / "examples/fsr4_demo_main.c", "FSR4 Native Demo",
                     libraries=[library])
    print(json.dumps(dict(package=str(out / "PPSA88900"), pipeline_cache=cache.exists()), indent=2))


if __name__ == "__main__":
    main()
