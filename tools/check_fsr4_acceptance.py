#!/usr/bin/env python3
# Copyright (C) 2026 BlackBearReloaded
# SPDX-License-Identifier: GPL-3.0-or-later
"""Accept a native FSR4 runtime run against an independent Vulkan control run.

Both runs replay the same captured fixture through the same SPIR-V and report,
per frame, the PSNR of their output against the WARP reference. FSR4's INT8
quantization turns last-bit floating-point differences into different
quantized values, so no conformant driver reproduces WARP bit-exactly. A run is
accepted when it completed, produced only finite values and, on every frame,
is at least as close to the reference as the control, less MARGIN_DB.
"""
import argparse
from pathlib import Path
import re
import sys

MARGIN_DB = 1.0


def parse(text):
    """Parse fsr4-rt-result.txt into its begin fields, frames and end state."""
    run = {"begin": None, "frames": [], "complete": False}
    for line in text.splitlines():
        fields = dict(re.findall(r"(\w+)=(\S+)", line))
        if line.startswith("FSR4_RT_BEGIN "):
            run["begin"] = fields
        elif line.startswith("FSR4_RT_FRAME "):
            run["frames"].append({"frame": int(fields["frame"]), "psnr": float(fields["psnr"]),
                                  "nonfinite": int(fields["nonfinite"])})
        elif line.startswith("FSR4_RT_END "):
            run["complete"] = fields.get("result") == "COMPLETE"
    return run


def evaluate(device, control, margin=MARGIN_DB):
    """Return (accepted, report lines) for a device run against a control run."""
    problems, lines = [], []
    for name, run in (("device", device), ("control", control)):
        if not run["begin"] or not run["complete"]:
            problems.append(f"{name} run is incomplete")
    if device["begin"] and control["begin"]:
        for key in ("fixture", "scenario", "frames", "render"):
            if device["begin"].get(key) != control["begin"].get(key):
                problems.append(f"{key} differs: {device['begin'].get(key)} != {control['begin'].get(key)}")
    if [f["frame"] for f in device["frames"]] != [f["frame"] for f in control["frames"]] or not device["frames"]:
        problems.append("frame lists differ or are empty")
    else:
        for d, c in zip(device["frames"], control["frames"]):
            ok = d["nonfinite"] == 0 and d["psnr"] >= c["psnr"] - margin
            lines.append(f"frame {d['frame']}: device {d['psnr']:.2f} dB, control {c['psnr']:.2f} dB, "
                         f"nonfinite {d['nonfinite']} {'ok' if ok else 'FAIL'}")
            if not ok:
                problems.append(f"frame {d['frame']} fails")
    return not problems, lines + problems


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("device", type=Path, help="fsr4-rt-result.txt of the run under test")
    parser.add_argument("control", type=Path, help="fsr4-rt-result.txt of the control run (lavapipe)")
    args = parser.parse_args()
    accepted, lines = evaluate(parse(args.device.read_text()), parse(args.control.read_text()))
    print("\n".join(lines))
    print("ACCEPTED" if accepted else "REJECTED")
    return 0 if accepted else 1


if __name__ == "__main__":
    sys.exit(main())
