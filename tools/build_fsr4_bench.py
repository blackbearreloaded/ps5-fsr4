#!/usr/bin/env python3
# Copyright (C) 2026 BlackBearReloaded
# SPDX-License-Identifier: GPL-3.0-or-later
"""Build the headless native FSR4 benchmark (PPSA88900) against the staged ps5_fsr4 SDK.

It times 720p-to-1080p upscaling from submission to fence, with a per-pass
profile, and needs neither a display nor a capture. Stage the SDK first with
make driver-sdk and tools/build_fsr4_sdk.py.
"""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from fsr4_paths import DIST_SDK  # noqa: E402
from build_fsr4_clear import build_native_app  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pipeline-cache", type=Path, default=DIST_SDK / "share/ps5fsr4/pipeline-cache-ps5.bin",
                        help="Pipeline cache data from an earlier run on this driver")
    parser.add_argument("--out", type=Path, default=ROOT / "build/fsr4-bench")
    args = parser.parse_args()
    library = DIST_SDK / "lib/libps5_fsr4.a"
    if not library.is_file():
        raise SystemExit(f"{library} is missing; run tools/build_fsr4_sdk.py")
    out = args.out.resolve()
    assets = out / "PPSA88900/assets"
    assets.mkdir(parents=True, exist_ok=True)
    cache = assets / "pipeline-cache.bin"
    if args.pipeline_cache and args.pipeline_cache.is_file():
        cache.write_bytes(args.pipeline_cache.read_bytes())
    elif cache.exists():
        cache.unlink()
    build_native_app(out, ROOT / "examples/fsr4_bench_main.c", "FSR4 Native Benchmark", libraries=[library])
    print(json.dumps(dict(package=str(out / "PPSA88900"), pipeline_cache=cache.exists()), indent=2))


if __name__ == "__main__":
    main()
