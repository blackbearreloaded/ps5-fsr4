#!/usr/bin/env python3
# Copyright (C) 2026 BlackBearReloaded
# SPDX-License-Identifier: GPL-3.0-or-later
"""Inventory DXIL containers in the exact signed AMD provider without exporting code."""
import argparse
from collections import Counter
import hashlib
import json
import re
from pathlib import Path
import struct

PROVIDER_SHA256 = "d0dcccc74a43c44ba435b7a369b456e0970d8a4464e4bd683119b374f2c9fb46"
PROVIDER_BYTES = 28761864
MODEL_NAME = re.compile(rb"fsr4_model_v07_fp8_no_scale_[a-z0-9_]+")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def container(data, offset):
    if data[offset:offset + 4] != b"DXBC" or offset + 32 > len(data):
        return None
    version, size, count = struct.unpack_from("<III", data, offset + 20)
    if version != 1 or size < 32 or size > len(data) - offset or count > 32:
        return None
    if 32 + 4 * count > size:
        return None
    tags = []
    for i in range(count):
        chunk = struct.unpack_from("<I", data, offset + 32 + 4 * i)[0]
        if chunk + 8 > size:
            return None
        length = struct.unpack_from("<I", data, offset + chunk + 4)[0]
        if length > size - chunk - 8:
            return None
        tag = data[offset + chunk:offset + chunk + 4]
        if not all(32 <= byte < 127 for byte in tag):
            return None
        tags.append(tag.decode("ascii"))
    if "DXIL" not in tags:
        return None
    blob = data[offset:offset + size]
    names = {name.decode("ascii") for name in MODEL_NAME.findall(blob)}
    if len(names) > 1:
        raise ValueError(f"Multiple model names in DXIL container at {offset}")
    return {"offset": offset, "bytes": size, "sha256": digest(blob),
            "chunks": tags, "embedded_model_entry": next(iter(names), None)}


def scan(data):
    records = []
    start = 0
    while (offset := data.find(b"DXBC", start)) != -1:
        record = container(data, offset)
        if record:
            if records and offset < records[-1]["offset"] + records[-1]["bytes"]:
                raise ValueError("Overlapping DXIL containers")
            records.append(record)
        start = offset + 4
    return records


def inventory(data, manifest):
    if len(data) != PROVIDER_BYTES or digest(data) != PROVIDER_SHA256:
        raise ValueError("Signed AMD provider identity mismatch")
    if manifest.get("sdk_sha256") != PROVIDER_SHA256:
        raise ValueError("BC250 manifest references a different provider")
    rows = manifest["replacements"]
    if len(rows) != 348 or len({row["original_offset"] for row in rows}) != 348:
        raise ValueError("Incomplete BC250 replacement inventory")
    records = scan(data)
    if len(records) != 1028:
        raise ValueError(f"Unexpected DXIL container count: {len(records)}")
    by_offset = {record["offset"]: record for record in records}
    for row in rows:
        record = by_offset.get(row["original_offset"])
        if not record or record["bytes"] != row["original_size"] or record["sha256"] != row["original_sha256"]:
            raise ValueError(f"BC250 shader identity mismatch: {row['original_offset']}")
        if record["embedded_model_entry"] != row["entry"]:
            raise ValueError(f"Model entry mismatch at {row['original_offset']}")
        record["bc250_model_entry"] = row["entry"]
    model_counts = Counter(record["embedded_model_entry"] for record in records
                           if record["embedded_model_entry"])
    chunk_profiles = Counter(",".join(record["chunks"]) for record in records)
    return {
        "schema": "ps5-fsr4-provider-dxil-inventory/1",
        "scope": "static signed-DLL containers; no runtime dispatch, model weights or PS5 execution",
        "provider_sha256": PROVIDER_SHA256,
        "total_containers": len(records),
        "bc250_replacement_containers": len(rows),
        "other_containers": len(records) - len(rows),
        "named_model_containers": sum(model_counts.values()),
        "named_model_families": len(model_counts),
        "model_entry_counts": dict(sorted(model_counts.items())),
        "chunk_profiles": dict(sorted(chunk_profiles.items())),
        "records": records,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dll", type=Path, default=Path("build/fsr4-reference/amd_fidelityfx_upscaler_dx12.dll"))
    parser.add_argument("--manifest", type=Path, default=Path("../references/bc250-fsr4-fork/dll/manifest.json"))
    parser.add_argument("--output", type=Path, default=Path("build/fsr4-provider-inventory.json"))
    args = parser.parse_args()
    result = inventory(args.dll.read_bytes(), json.loads(args.manifest.read_text()))
    result["bc250_manifest_sha256"] = digest(args.manifest.read_bytes())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(f"{result['total_containers']} DXIL containers: "
          f"{result['bc250_replacement_containers']} BC250 model replacements, "
          f"{result['other_containers']} other containers; "
          f"{result['named_model_containers']} named model containers in "
          f"{result['named_model_families']} families; {args.output}")


if __name__ == "__main__":
    main()
