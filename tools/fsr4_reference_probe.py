#!/usr/bin/env python3
# Copyright (C) 2026 BlackBearReloaded
# SPDX-License-Identifier: GPL-3.0-or-later
"""Build and optionally run the pinned BC250 workload on Windows WARP via WSL."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import struct
import subprocess

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "build/reference-runtime"
PROBE_SHA = "b7a69169801545793c7f85457b0a6d128fdf9324198b46b2f93e959a54307af4"
PROVIDER_SHA = "8192ea97620f8e6407bff346bf905f0d555ff73d89fe14616eb1fa5e41ab3175"
SCALAR_UNPACK_PROVIDER_SHA = "bef24445af655965b06115bcee7b570f89b753e856f650c4a00478b7916ec372"
RUNTIME_HASHES = {'d3d10warp.dll': 'e79c10550449365adf0a9393d97a0df69941e671ab6e952a78d92da066517ca3', 'D3D12Core.dll': '37fa14281a58cc834076971873006feb8a8d25cddc908d1a345bda1b149ffc7d', 'D3D12StateObjectCompiler.dll': '023ba81d3a9c37ae805b746bb9c77bf2f6659044ee2fef2604154b507faca817', 'd3d12SDKLayers.dll': '7ab48d56d1c18708ebcf16c7c9259b2d783e2b935b85add4a244573b9f03cab4'}

RENDERDOC_HASHES = {
    "renderdoc.dll": "809da38e3867d9fd09cc5c30dd5310500dee75e999166d7a14ad1ef6e9eca65d",
    "renderdoc_app.h": "b7005e7dc34c3635046868bbd76d81b9b055aede0f56daa0bd39fedee0639ffb",
}
REFERENCE_OUTPUT_SHA = "1a06dd8e1be3aac817a8a17abcb3dc9358e41b0821fb3c210993878336e04799"
SCALAR_UNPACK_OUTPUT_SHA = "ed7f85a7edf0c90cb86cbddbfa361ac1ad8d2f41d7714752344059f1d0965860"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check_output(log, pixels, width=192, height=144, frames=4):
    """Execution/readback sanity only; this does not prove image accuracy."""
    if not (16 <= width <= 3840 and 16 <= height <= 2160 and 1 <= frames <= 600):
        raise ValueError("Workload exceeds pinned probe bounds")
    required = ["provider: status=0 id=17700776140664213505 name=4.1.1r11",
                f"readback: {width}x{height} RGBA32F, {frames} completed frames", "destroy: status=0"]
    required += [f"completed: frame={i} fence={i+1}" for i in range(frames)]
    required += [f"dispatch: frame={i} status=0" for i in range(frames)]
    if any(line not in log.splitlines() for line in required) or "FAILED " in log:
        raise ValueError("Incomplete FSR4 execution (FSR3 fallback is not accepted)")
    if len(pixels) != width * height * 16:
        raise ValueError("Reference output size mismatch")
    minimum, maximum = math.inf, -math.inf
    for (value,) in struct.iter_unpack("<f", pixels):
        if not math.isfinite(value):
            raise ValueError("Reference output contains non-finite values")
        minimum, maximum = min(minimum, value), max(maximum, value)
    return dict(bytes=len(pixels), sha256=hashlib.sha256(pixels).hexdigest(),
                finite_values=len(pixels) // 4, minimum=minimum, maximum=maximum,
                image_accuracy_verified=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", type=Path, default=RUNTIME,
                        help="Prepared local Wine headers/libs, FFX headers, and app-local runtimes")
    parser.add_argument("--probe", type=Path, default=ROOT.parent /
                        "references/bc250-fsr4-fork/dll/probe/provider_probe.c")
    parser.add_argument("--provider", type=Path, default=RUNTIME /
                        "bc250-rc11/amd_fidelityfx_upscaler_dx12.dll")
    parser.add_argument("--out", type=Path, default=RUNTIME / "reproducible")
    parser.add_argument("--run", action="store_true", help="Execute Windows WARP through WSL interop")
    parser.add_argument("--renderdoc", type=Path,
                        help="Optional pinned RenderDoc 1.46 x64 directory; records all four frames")
    parser.add_argument("--render-size", type=int, nargs=2, metavar=("WIDTH", "HEIGHT"),
                        default=[128, 96])
    parser.add_argument("--output-size", type=int, nargs=2, metavar=("WIDTH", "HEIGHT"),
                        default=[192, 144])
    parser.add_argument("--frames", type=int, default=4)
    parser.add_argument("--scenario", choices=["static", "sdr", "hdr", "motion", "reset", "resize", "rcas"],
                        default="static", help="Synthetic workload implemented by the pinned probe")
    args = parser.parse_args()
    for size in (args.render_size, args.output_size):
        if not (16 <= size[0] <= 3840 and 16 <= size[1] <= 2160):
            parser.error("Probe sizes must be 16..3840 by 16..2160")
    if not 1 <= args.frames <= 600:
        parser.error("Probe frame count must be 1..600")
    if any(a > b for a, b in zip(args.render_size, args.output_size)):
        parser.error("Render size must not exceed output size")
    workload = dict(render_size=args.render_size, output_size=args.output_size,
                    frames=args.frames, scenario=args.scenario)
    pinned_workload = workload == dict(render_size=[128, 96], output_size=[192, 144],
                                       frames=4, scenario="static")
    renderdoc = args.renderdoc.resolve() if args.renderdoc else None
    if renderdoc:
        for name, expected in RENDERDOC_HASHES.items():
            if sha(renderdoc / name) != expected:
                raise ValueError(f"RenderDoc identity mismatch: {name}")
    inputs, out, probe = args.inputs.resolve(), args.out.resolve(), args.probe.resolve()
    if sha(probe) != PROBE_SHA or sha(args.provider) not in (PROVIDER_SHA, SCALAR_UNPACK_PROVIDER_SHA):
        raise ValueError("Reference source/provider identity mismatch")
    # All inputs are explicit local dependencies. No downloads or global installation.
    wine = inputs / "prefix/usr"
    ffx = inputs / "sdk/Kits/FidelityFX/upscalers/include"
    runtime_files = [inputs / "native-windows/d3d10warp.dll"]
    runtime_files += sorted((inputs / "native-windows/D3D12").glob("*.dll"))
    if not (inputs / "native-windows/D3D12/D3D12Core.dll").is_file():
        raise ValueError("App-local Agility runtime missing")
    if {p.name for p in runtime_files} != set(RUNTIME_HASHES):
        raise ValueError("Pinned runtime inventory differs")
    for path in runtime_files:
        if sha(path) != RUNTIME_HASHES[path.name]:
            raise ValueError(f"Runtime identity mismatch: {path}")
    out.mkdir(parents=True, exist_ok=True)
    (out / "D3D12").mkdir(exist_ok=True)
    for path in runtime_files:
        target = out / path.name if path.name.lower() == "d3d10warp.dll" else out / "D3D12" / path.name
        shutil.copyfile(path, target)
    exe = out / "fsr4-reference-probe.exe"
    command = ["clang-18", "--target=x86_64-w64-windows-gnu", "-fms-extensions",
               "-D__WINE_USE_MSVCRT", "-DTH32CS_SNAPMODULE32=0x10",
               "-DBC250_PROBE_BARRIER_AUDIT", f'-DBC250_PROBE_SOURCE="{probe}"',
               "-isystem", str(wine / "include/wine/wine/msvcrt"),
               "-isystem", str(wine / "include/wine/wine/windows"),
               "-I" + str(ffx), "-O2", "-Wall", "-Wextra", "-Werror", "-nostdlib",
               "--ld-path=/usr/bin/ld.lld-18", "-Wl,--entry,mainCRTStartup",
               "-Wl,--subsystem,console", "-Wl,--no-insert-timestamp",
               "-L" + str(wine / "lib/x86_64-linux-gnu/wine/x86_64-windows"),
               str(ROOT / "tools/fsr4_reference_probe.c"), "-lkernel32", "-lucrtbase",
               "-o", str(exe)]
    if renderdoc:
        command += ["-DFSR4_CAPTURE", "-I" + str(renderdoc)]
    subprocess.run(command, check=True)
    manifest = dict(executable_sha256=sha(exe), probe_source_sha256=PROBE_SHA,
                    provider_sha256=sha(args.provider),
                    reference_variant=("scalar-unpack" if sha(args.provider) == SCALAR_UNPACK_PROVIDER_SHA else "upstream-rc11"),
                    backend="Windows WARP via WSL interop",
                    compiler=subprocess.check_output(["clang-18", "--version"], text=True).splitlines()[0],
                    ps5_execution=False, graph_capture_complete=False, workload=workload,
                    runtime_sha256={p.name: sha(p) for p in runtime_files})
    if renderdoc:
        manifest["capture_tool"] = dict(version="RenderDoc 1.46", sha256=RENDERDOC_HASHES)
    (out / "build.json").write_text(json.dumps(manifest, indent=2) + "\n")
    if not args.run:
        print(exe)
        return
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    run = out / stamp
    run.mkdir()
    def win(path):
        return subprocess.check_output(["wslpath", "-w", str(path)], text=True).strip()
    env = dict(os.environ, BC250_FFX_DLL=win(args.provider.resolve()),
               BC250_FFX_OUTPUT=win(run / "probe.txt"),
               BC250_FFX_PIXELS=win(run / "output.rgba32f"), BC250_FFX_TRACE="1")
    fixed = dict(RENDER_W=str(args.render_size[0]), RENDER_H=str(args.render_size[1]),
                 OUTPUT_W=str(args.output_size[0]), OUTPUT_H=str(args.output_size[1]),
                 MAX_OUTPUT_W=str(args.output_size[0]), MAX_OUTPUT_H=str(args.output_size[1]),
                 FRAMES=str(args.frames), SCENARIO=args.scenario)
    env.update({"BC250_FFX_" + key: value for key, value in fixed.items()})
    exported = ["BC250_FFX_DLL", "BC250_FFX_OUTPUT", "BC250_FFX_PIXELS", "BC250_FFX_TRACE"]
    exported += ["BC250_FFX_" + key for key in fixed]
    if renderdoc:
        env["FSR4_RENDERDOC_DLL"] = win(renderdoc / "renderdoc.dll")
        exported.append("FSR4_RENDERDOC_DLL")
    env["WSLENV"] = ":".join(filter(None, [env.get("WSLENV", ""), *exported]))
    with (run / "console.log").open("w") as console:
        result = subprocess.run([str(exe)], cwd=out, env=env,
                                stdout=console, stderr=subprocess.STDOUT)
    manifest["process_exit"] = result.returncode
    (run / "run.json").write_text(json.dumps(manifest, indent=2) + "\n")
    if result.returncode:
        raise RuntimeError(f"Reference probe failed: {result.returncode}; {run}")
    log = (run / "probe.txt").read_text()
    actual_workload = (f"workload: render={args.render_size[0]}x{args.render_size[1]} "
                       f"output={args.output_size[0]}x{args.output_size[1]} "
                       f"frames={args.frames} scenario={args.scenario}")
    if actual_workload not in log.splitlines():
        raise RuntimeError(f"Probe did not execute the requested workload: {run}")
    manifest["readback"] = check_output(log, (run / "output.rgba32f").read_bytes(),
                                         *args.output_size, args.frames)
    if renderdoc:
        captures = list(run.glob("*.rdc"))
        if ("capture: complete=1" not in (run / "probe.txt").read_text().splitlines()
                or len(captures) != 1 or captures[0].stat().st_size == 0):
            raise RuntimeError(f"Capture did not complete: {run}")
        expected_output = (SCALAR_UNPACK_OUTPUT_SHA if manifest["reference_variant"] == "scalar-unpack"
                           else REFERENCE_OUTPUT_SHA)
        if pinned_workload and manifest["readback"]["sha256"] != expected_output:
            raise RuntimeError(f"Captured output differs from the pinned reference: {run}")
        manifest["capture"] = dict(file=captures[0].name, sha256=sha(captures[0]),
                                   bytes=captures[0].stat().st_size,
                                   output_matches_uncaptured_reference=True if pinned_workload else None)
    manifest["log_sha256"] = sha(run / "probe.txt")
    (run / "run.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(dict(run=str(run), readback=manifest["readback"]), indent=2))


if __name__ == "__main__":
    main()
