#!/usr/bin/env python3
# Copyright (C) 2026 BlackBearReloaded
# SPDX-License-Identifier: GPL-3.0-or-later
"""Package the FSR4 runtime validation app with per-frame reference inputs.

Unlike the captured replay, the app records the complete runtime dispatch from
API-level parameters; the capture supplies only application inputs and the
reference output of every frame.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from build_fsr4_frame import blob, load_capture  # noqa: E402
from build_fsr4_runtime import roles  # noqa: E402

SOURCES = [ROOT / "src/fsr4/ps5_fsr4.c"]
INCLUDES = [ROOT / "include", ROOT / "src/fsr4"]
# Dispatch parameters of the pinned reference probe (references/bc250-fsr4-fork/dll/probe/provider_probe.c).
FLAGS = {"sdr": "PS5FSR4_FLAG_AUTO_EXPOSURE"}


def probe_parameters(scenario, frame, render):
    jitter = (0.0, 0.0)
    if scenario == "motion":
        jitter = ((frame % 4) * .25 - .375, (frame % 3) * .3333333 - .3333333)
    width, height = render
    if scenario == "resize" and frame % 4 >= 2:
        width, height = width * 3 // 4, height * 3 // 4
    reset = frame == 0 or (scenario == "reset" and frame % 4 == 0)
    return jitter, (width, height), reset


def binding(dispatch, label, resource):
    for record in dispatch[label]:
        if record["descriptor"]["resource"] == resource:
            return record
    raise ValueError("Missing binding for " + resource)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", type=Path, default=ROOT / "build/reference-runtime/capture-export-scalar-unpack")
    parser.add_argument("--scenario", default="static")
    parser.add_argument("--frames", type=int, default=0, help="Limit frames (default: all captured)")
    parser.add_argument("--runtime", type=Path, default=ROOT / "build/fsr4-runtime")
    parser.add_argument("--out", type=Path, default=ROOT / "build/fsr4-runtime-test")
    parser.add_argument("--host", action="store_true", help="Build a host binary instead of the PS5 package")
    parser.add_argument("--pipeline-cache", type=Path, help="Pipeline cache data saved by an earlier run on the same driver")
    args = parser.parse_args()
    capture = args.capture.resolve()
    graph = load_capture(capture, json.loads((capture / "complete.json").read_text())["graph_sha256"])
    dispatches = graph["dispatches"]
    textures = {t["resourceId"]: t for t in graph["textures"]}
    frames = len(dispatches) // 28 if not args.frames else args.frames
    out = args.out.resolve()
    assets = out / ("assets" if args.host else "PPSA88900/assets")
    assets.mkdir(parents=True, exist_ok=True)
    ids = {role: rid for rid, role in roles(graph).items()}
    color0, output_tex = textures[ids["COLOR"]], textures[ids["OUTPUT"]]
    digest = hashlib.sha256()
    rows = []
    for f in range(frames):
        pre, post = dispatches[28 * f + 1], dispatches[28 * f + 27]
        files = {"color": binding(dispatches[28 * f], "srv", ids["COLOR"])["at_event"],
                 "depth": binding(pre, "srv", ids["DEPTH"])["at_event"],
                 "motion": binding(pre, "srv", ids["MOTION_VECTORS"])["at_event"],
                 "expected": binding(post, "uav", ids["OUTPUT"])["at_event"]}
        for kind, record in files.items():
            data = blob(capture, record)
            (assets / f"frame{f}-{kind}.bin").write_bytes(data)
            digest.update(data)
        (jx, jy), (rw, rh), reset = probe_parameters(args.scenario, f, (color0["width"], color0["height"]))
        rows.append("{%r,%r,%d,%d,%du}" % (jx, jy, rw, rh, int(reset)))
    if args.pipeline_cache:
        (assets / "pipeline-cache.bin").write_bytes(args.pipeline_cache.read_bytes())
    fixture_id = digest.hexdigest()
    header = "\n".join([
        "#include <stdint.h>",
        f"#define FSR4_RT_ID \"{fixture_id}\"",
        f"#define FSR4_RT_SCENARIO \"{args.scenario}\"",
        f"#define FSR4_RT_FRAMES {frames}u",
        f"#define FSR4_RT_RENDER_WIDTH {color0['width']}u",
        f"#define FSR4_RT_RENDER_HEIGHT {color0['height']}u",
        f"#define FSR4_RT_OUTPUT_WIDTH {output_tex['width']}u",
        f"#define FSR4_RT_OUTPUT_HEIGHT {output_tex['height']}u",
        "#define FSR4_RT_FLAGS (%s)" % FLAGS.get(args.scenario,
                                                "PS5FSR4_FLAG_HIGH_DYNAMIC_RANGE|PS5FSR4_FLAG_AUTO_EXPOSURE"),
        "static const struct { float jitter_x, jitter_y; uint32_t render_width, render_height, reset; } "
        "fsr4_rt_frames[] = {" + ",".join(rows) + "};", ""])
    (out / "fsr4_runtime_fixture.h").write_text(header)
    source = ROOT / "examples/native_consumer/fsr4_runtime_test.c"
    includes = [*INCLUDES, args.runtime.resolve()]
    if args.host:
        vulkan = ROOT / "third_party/vulkan-headers/include"
        subprocess.run(["cc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror", "-DFSR4_HOST", "-D_DEFAULT_SOURCE",
                        "-I" + str(out), "-I" + str(vulkan), *("-I" + str(x) for x in includes),
                        str(source), *map(str, SOURCES), "/usr/lib/x86_64-linux-gnu/libvulkan.so.1", "-lm",
                        "-o", str(out / "fsr4_runtime_test")], check=True)
        print(json.dumps(dict(binary=str(out / "fsr4_runtime_test"), assets=str(assets),
                              fixture_id=fixture_id, frames=frames), indent=2))
        return
    from build_fsr4_clear import build_native_app
    build_native_app(out, source, "FSR4 Runtime Test", extra_sources=SOURCES, include_dirs=includes)
    print(json.dumps(dict(package=str(out / "PPSA88900"), fixture_id=fixture_id, frames=frames), indent=2))


if __name__ == "__main__":
    main()
