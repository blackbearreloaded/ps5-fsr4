#!/usr/bin/env python3
# Copyright (C) 2026 BlackBearReloaded
# SPDX-License-Identifier: GPL-3.0-or-later
"""Verify the signed provider's thirteen post-stage families only clear buffer values."""
import argparse
from collections import Counter
import json
from pathlib import Path
import re
import struct
import subprocess

if __package__:
    from .fsr4_provider_inventory import inventory
else:
    from fsr4_provider_inventory import inventory

BITCODE = b"BC\xc0\xde"
CALL = re.compile(r"call [^\n]*@dx\.op\.([A-Za-z0-9.]+)")
STORE = re.compile(r"call void @dx\.op\.rawBufferStore\.i32\([^\n]+")
ZERO_STORE = re.compile(r", i32 0, i32 0, i32 0, i32 0, i8 15, i32 4\)$")
ALLOWED = {"annotateHandle", "binary.i32", "cbufferLoadLegacy.i32",
           "createHandle", "createHandleFromBinding", "rawBufferStore.i32",
           "tertiary.i32", "threadId.i32"}


def bitcode(data, record):
    base = record["offset"]
    count = struct.unpack_from("<I", data, base + 28)[0]
    for i in range(count):
        offset = struct.unpack_from("<I", data, base + 32 + i * 4)[0]
        if data[base + offset:base + offset + 4] != b"DXIL":
            continue
        size = struct.unpack_from("<I", data, base + offset + 4)[0]
        payload = data[base + offset + 8:base + offset + 8 + size]
        index = payload.find(BITCODE)
        if index < 0:
            break
        return payload[index:]
    raise ValueError(f"Missing DXIL bitcode at {base}")


def summarize_ir(ir, entry):
    marker = f"define void @{entry}()"
    if ir.count(marker) != 1:
        raise ValueError(f"Missing or ambiguous entrypoint: {entry}")
    body = ir.split(marker, 1)[1].split("\n}\n", 1)[0]
    calls = Counter(CALL.findall(body))
    all_calls = re.findall(r"\bcall [^\n]*@([^\(]+)\(", body)
    if len(all_calls) != sum(calls.values()) or re.search(r"^\s*(?:store|atomicrmw|cmpxchg)\b", body, re.M):
        raise ValueError("Unclassified post-stage side effect")
    if set(calls) - ALLOWED:
        raise ValueError(f"Unexpected post-stage operation: {set(calls) - ALLOWED}")
    stores = STORE.findall(body)
    if not stores or len(stores) != calls["rawBufferStore.i32"]:
        raise ValueError("Missing or unaccounted post-stage store")
    if not all(ZERO_STORE.search(store) for store in stores):
        raise ValueError("Post-stage store is not a four-word zero write")
    if calls["cbufferLoadLegacy.i32"] != 1 or calls["threadId.i32"] != 1:
        raise ValueError("Unexpected post-stage input shape")
    return {"zero_stores": len(stores), "operations": dict(sorted(calls.items()))}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dll", type=Path, default=Path("build/fsr4-reference/amd_fidelityfx_upscaler_dx12.dll"))
    parser.add_argument("--manifest", type=Path, default=Path("../references/bc250-fsr4-fork/dll/manifest.json"))
    parser.add_argument("--output", type=Path, default=Path("build/fsr4-poststage-audit.json"))
    args = parser.parse_args()
    data = args.dll.read_bytes()
    provider = inventory(data, json.loads(args.manifest.read_text()))
    records = []
    for row in provider["records"]:
        entry = row["embedded_model_entry"] or ""
        if not re.fullmatch(r"fsr4_model_v07_fp8_no_scale_pass(?:[0-9]|1[0-2])_post", entry):
            continue
        result = subprocess.run(["llvm-dis-18", "-o", "-", "-"],
                                input=bitcode(data, row), capture_output=True,
                                timeout=30, check=True)
        audit = summarize_ir(result.stdout.decode(), entry)
        records.append({"offset": row["offset"], "sha256": row["sha256"],
                        "entry": entry, **audit})
    families = Counter(row["entry"] for row in records)
    if len(records) != 117 or len(families) != 13 or set(families.values()) != {9}:
        raise ValueError("Incomplete post-stage family inventory")
    output = {
        "schema": "ps5-fsr4-poststage-static-audit/1",
        "scope": "DXIL static operations only; exact write ranges and runtime use unverified",
        "provider_sha256": provider["provider_sha256"],
        "families": dict(sorted(families.items())),
        "total_zero_store_calls": sum(row["zero_stores"] for row in records),
        "records": records,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, indent=2) + "\n")
    print(f"{len(records)} post-stage variants in {len(families)} families: "
          f"{output['total_zero_store_calls']} zero-valued stores; {args.output}")


if __name__ == "__main__":
    main()
