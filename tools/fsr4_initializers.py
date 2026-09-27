#!/usr/bin/env python3
# Copyright (C) 2026 BlackBearReloaded
# SPDX-License-Identifier: GPL-3.0-or-later
"""Recover initializer bytes from the exact pinned provider's six-way getter."""
import argparse
import json
from pathlib import Path
import struct

if __package__:
    from .fsr4_provider_inventory import PROVIDER_BYTES, PROVIDER_SHA256, digest
else:
    from fsr4_provider_inventory import PROVIDER_BYTES, PROVIDER_SHA256, digest

# Fixed PE section mapping for this hash only; this is not a generic PE loader.
TEXT_RVA, TEXT_RAW, TEXT_SIZE = 0x1000, 0x400, 0xA78DB
RDATA_RVA, RDATA_RAW, RDATA_SIZE = 0xA9000, 0xA7E00, 0x1A55FA2
GETTER_RVA, TABLE_RVA = 0x9600, 0x96E0
STANDARD = "ce1bd5f19d4fc14f857c5fc810def7f45d5a935a62aa2ca5b5a59829a1d6c868"
DISTINCT = "101d91f69e3f6121c2a6eb477ee5cbf6e7bf25f26b65c0a4dcd1ac04e57fe2e8"
# Observed t18 SRV in the four-frame RC11 WARP capture, matched to the signed DLL.
RUNTIME_MODEL = dict(offset=0xC04A00, bytes=131072,
                     sha256="a54e552ff69a3f7199861417f7ae461d1f67f636ab532c6ec9d8ea5d2963735a",
                     binding="t18, space0", capture_resource="ResourceId::296",
                     workload="RC11 128x96 to 192x144; four static frames")


def decode_case(code, rva):
    """Decode only the observed mov-size / lea-pointer / return pair."""
    if (len(code) != 27 or code[0] != 0xB8 or
            code[5:8] != bytes.fromhex("48 8d 0d") or
            code[12:] != bytes.fromhex("48 89 42 08 48 8b c2 48 89 0a 48 83 c4 18 c3")):
        raise ValueError("Initializer getter case instructions differ")
    size = struct.unpack_from("<I", code, 1)[0]
    target = rva + 12 + struct.unpack_from("<i", code, 8)[0]
    if size != 131072 or not RDATA_RVA <= target <= RDATA_RVA + RDATA_SIZE - size:
        raise ValueError("Initializer size or read-only section bounds differ")
    return target - RDATA_RVA + RDATA_RAW, size


def inventory(data):
    if len(data) != PROVIDER_BYTES or digest(data) != PROVIDER_SHA256:
        raise ValueError("Signed AMD provider identity mismatch")
    start = GETTER_RVA - TEXT_RVA + TEXT_RAW
    # Includes unsigned selector <= 5 check, image base and six-entry jump table.
    prefix = bytes.fromhex(
        "48 83 ec 18 41 83 f8 05 0f 87 b9 00 00 00 41 8b c0 "
        "4c 8d 05 e8 69 ff ff 41 8b 8c 80 e0 96 00 00 49 03 c8 ff e1")
    if data[start:start + len(prefix)] != prefix:
        raise ValueError("Initializer selector instructions differ")
    table = TABLE_RVA - TEXT_RVA + TEXT_RAW
    records = []
    for selector in range(6):
        rva = struct.unpack_from("<I", data, table + selector * 4)[0]
        if not TEXT_RVA <= rva <= TEXT_RVA + TEXT_SIZE - 27:
            raise ValueError("Initializer case outside code section")
        raw = rva - TEXT_RVA + TEXT_RAW
        offset, size = decode_case(data[raw:raw + 27], rva)
        sha = digest(data[offset:offset + size])
        if sha != (DISTINCT if selector == 5 else STANDARD):
            raise ValueError("Initializer content identity mismatch")
        records.append(dict(selector=selector, case_rva=rva, offset=offset,
                            bytes=size, sha256=sha))
    row = RUNTIME_MODEL
    if digest(data[row["offset"]:row["offset"] + row["bytes"]]) != row["sha256"]:
        raise ValueError("Runtime-observed model identity mismatch")
    return dict(schema="ps5-fsr4-initializers/1", provider_sha256=PROVIDER_SHA256,
                evidence="static getter and exact bytes; no observed GPU binding",
                getter_rva=GETTER_RVA, unique_buffers=2, records=records,
                runtime_model=dict(RUNTIME_MODEL),
                runtime_mode_mapping_verified=False, graph_complete=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dll", type=Path,
                        default=Path("build/fsr4-reference/amd_fidelityfx_upscaler_dx12.dll"))
    parser.add_argument("--out", type=Path, default=Path("build/fsr4-initializers.json"))
    parser.add_argument("--extract-dir", type=Path,
                        help="Optional local output for two getter buffers and the captured model; not redistribution")
    args = parser.parse_args()
    data = args.dll.read_bytes()
    report = inventory(data)  # Validate every record before writing any output.
    if args.extract_dir:
        args.extract_dir.mkdir(parents=True, exist_ok=True)
        for row in [*report["records"], report["runtime_model"]]:
            path = args.extract_dir / (row["sha256"] + ".bin")
            path.write_bytes(data[row["offset"]:row["offset"] + row["bytes"]])
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2) + "\n")
    print(f"Verified 6 getter selections / 2 unique buffers + 1 runtime model: {args.out}")


if __name__ == "__main__":
    main()
