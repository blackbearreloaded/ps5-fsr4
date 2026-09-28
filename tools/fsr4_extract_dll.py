#!/usr/bin/env python3
# Copyright (C) 2026 BlackBearReloaded
# SPDX-License-Identifier: GPL-3.0-or-later
"""Copy one FSR4 shader family and its model out of the BC250 RC11 provider DLL.

The DLL is read as a file, never loaded. tools/fsr4_dll_map.json records, for the
configuration the runtime supports (linear HDR, auto exposure, render-resolution
motion vectors, non-inverted depth, no jitter cancellation), where the selected DXIL
containers and the 128 KiB INT8 model of each model and resolution band lie, and
their SHA-256; every copy is verified. The copies are AMD-derived: keep them local.

Families: standard (AMD modes 0-4) or ultra-performance (mode 5), each for band 0
(outputs up to 1920x1080) or band 1 (up to 3840x2160).
"""
import argparse
import hashlib
import json
from pathlib import Path
import struct

ROOT = Path(__file__).resolve().parents[1]
MAP = ROOT / "tools/fsr4_dll_map.json"
FAMILIES = ("standard-band0", "standard-band1", "ultra-performance-band0", "ultra-performance-band1")


def sections(image):
    if image[:2] != b"MZ":
        raise ValueError("not a PE image")
    pe = struct.unpack_from("<I", image, 0x3C)[0]
    if image[pe:pe + 4] != b"PE\0\0" or struct.unpack_from("<H", image, pe + 24)[0] != 0x20B:
        raise ValueError("expected a PE32+ image")
    count, optional = struct.unpack_from("<H", image, pe + 6)[0], struct.unpack_from("<H", image, pe + 20)[0]
    table = pe + 24 + optional
    return [struct.unpack_from("<IIII", image, table + 40 * i + 8) for i in range(count)]


def file_offset(image, rva):
    for virtual_size, start, raw_size, raw in sections(image):
        if start <= rva < start + max(virtual_size, raw_size):
            if rva - start >= raw_size:
                raise ValueError(f"RVA {rva:#x} lies in a zero-filled section tail")
            return raw + rva - start
    raise ValueError(f"RVA {rva:#x} lies outside the PE sections")


def checked(image, offset, record, label, dxil=True):
    data = image[offset:offset + record["bytes"]]
    if len(data) != record["bytes"] or hashlib.sha256(data).hexdigest() != record["sha256"]:
        raise ValueError(f"{label}: bytes at {offset:#x} do not match the map")
    if dxil and (data[:4] != b"DXBC" or struct.unpack_from("<I", data, 24)[0] != len(data)):
        raise ValueError(f"{label}: not a complete DXIL container")
    return data


def extract(dll, family):
    """{dispatch index: DXIL} for SPD (0), the model dispatches (1-27) and RCAS (28), and the model."""
    table = json.loads(MAP.read_text())
    if family not in table["families"]:
        raise ValueError(f"unknown family {family}; choose one of {', '.join(FAMILIES)}")
    image = Path(dll).read_bytes()
    if hashlib.sha256(image).hexdigest() != table["dll_sha256"]:
        raise ValueError("unsupported DLL: expected the BC250 RC11 provider "
                         f"(SHA-256 {table['dll_sha256']})")
    entry = table["families"][family]
    shaders = {0: checked(image, int(table["spd"]["file_offset"], 16), table["spd"], "spd")}
    for index, record in enumerate(entry["passes"], start=1):
        shaders[index] = checked(image, file_offset(image, int(record["rva"], 16)), record, record["family"])
    shaders[len(shaders)] = checked(image, file_offset(image, int(table["rcas"]["rva"], 16)), table["rcas"], "rcas")
    model = checked(image, int(entry["model"]["file_offset"], 16), entry["model"], "model", dxil=False)
    return shaders, model


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("dll", type=Path, help="the BC250 RC11 amd_fidelityfx_upscaler_dx12.dll")
    parser.add_argument("out", type=Path, help="directory for pass<N>.dxil and model.bin")
    parser.add_argument("--family", choices=FAMILIES, default="standard-band0")
    args = parser.parse_args()
    shaders, model = extract(args.dll, args.family)
    args.out.mkdir(parents=True, exist_ok=True)
    for index, data in shaders.items():
        (args.out / f"pass{index}.dxil").write_bytes(data)
    (args.out / "model.bin").write_bytes(model)
    print(f"{args.family}: {len(shaders)} shaders and the model in {args.out}")


if __name__ == "__main__":
    main()
