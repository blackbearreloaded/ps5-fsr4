#!/usr/bin/env python3
"""Verify the pinned BC250 RC9 capsule and extract FSR4 compiler witnesses.

Inputs remain in the separately cloned BC250 project. Output is ignored build
data for local compiler experiments, never part of the SDK distribution.
"""
import argparse
import ctypes
import ctypes.util
import hashlib
import json
from pathlib import Path
import re
import tarfile

HEADER = "src/amd/vulkan/bc250_rc9_shaders.h"
PREFIX = "fsr4_model_v07_fp8_no_scale_"


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def decompress(frame, size):
    if not 0 < size <= 16 * 1024 * 1024:
        raise ValueError(f"Invalid SPIR-V size: {size}")
    library = ctypes.CDLL(ctypes.util.find_library("zstd") or "libzstd.so.1")
    library.ZSTD_decompress.argtypes = (
        ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t
    )
    library.ZSTD_decompress.restype = ctypes.c_size_t
    library.ZSTD_isError.argtypes = (ctypes.c_size_t,)
    library.ZSTD_isError.restype = ctypes.c_uint
    output = ctypes.create_string_buffer(size)
    source = ctypes.create_string_buffer(frame)
    result = library.ZSTD_decompress(output, size, source, len(frame))
    if library.ZSTD_isError(result) or result != size:
        raise ValueError(f"Zstandard decompression failed: {result} != {size}")
    return output.raw


def capsule(capsule_dir):
    manifest_bytes = (capsule_dir / "manifest.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    archive_bytes = (capsule_dir / "source-overlay.tar.xz").read_bytes()
    if sha256(archive_bytes) != manifest["source_overlay_sha256"]:
        raise ValueError("Capsule archive digest mismatch")
    with tarfile.open(capsule_dir / "source-overlay.tar.xz", "r:xz") as archive:
        names = {member.name for member in archive.getmembers()}
        if names != set(manifest["source_files"]):
            raise ValueError("Capsule members differ from manifest")
        for name, expected in manifest["source_files"].items():
            member = archive.getmember(name)
            if not member.isfile() or member.size != expected["bytes"]:
                raise ValueError(f"Invalid source member: {name}")
            data = archive.extractfile(member).read()
            if sha256(data) != expected["sha256"]:
                raise ValueError(f"Source digest mismatch: {name}")
            if name == HEADER:
                header = data
    marker = b"static const unsigned char bc250_rc9_blob[] = {"
    start = header.index(marker) + len(marker)
    end = header.index(b"};", start)
    blob = bytes.fromhex(b"".join(re.findall(rb"0x([0-9a-fA-F]{2})", header[start:end])).decode())
    if len(blob) != manifest["compressed_shader_bytes"]:
        raise ValueError("Compressed blob length mismatch")
    return manifest, blob, sha256(manifest_bytes)


def verified_shader(blob, entry, kind):
    offset, length, size, _ = entry[kind]
    if min(offset, length) < 0 or offset + length > len(blob):
        raise ValueError("Compressed shader lies outside capsule")
    spirv = decompress(blob[offset:offset + length], size)
    if len(spirv) % 4 or spirv[:4] != b"\x03\x02\x23\x07":
        raise ValueError("Malformed SPIR-V payload")
    if sha256(spirv) != entry[f"{kind}_spirv_sha256"]:
        raise ValueError(f"Shader digest mismatch: {entry['entry']} {kind}")
    return spirv


def witnesses(entries):
    def choose(stage, wave):
        matches = [
            (index, entry) for index, entry in enumerate(entries)
            if entry["entry"] == PREFIX + stage and entry["wave"] == wave
        ]
        if not matches:
            raise ValueError(f"Missing witness {stage} wave{wave}")
        return max(matches, key=lambda pair: pair[1]["target"][2])
    return [choose("prepass", 32), choose("pass9", 32),
            choose("postpass", 0)]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capsule_dir", type=Path)
    parser.add_argument("--output", type=Path, default=Path("build/fsr4-rc9-proof"))
    parser.add_argument("--all", action="store_true",
                        help="export all verified original/target shader pairs")
    args = parser.parse_args()
    manifest, blob, manifest_hash = capsule(args.capsule_dir)
    entries = manifest["shader_entries"]
    if len(entries) != 42:
        raise ValueError("Unexpected capsule entry count")
    checked = []
    for entry in entries:
        checked.append({
            "original": verified_shader(blob, entry, "original"),
            "target": verified_shader(blob, entry, "target"),
        })
    selected = list(enumerate(entries)) if args.all else witnesses(entries)
    args.output.mkdir(parents=True, exist_ok=True)
    exported = []
    for index, entry in selected:
        kinds = ("original", "target") if args.all else (
            "original" if entry["entry"] == PREFIX + "pass9" else "target",
        )
        for kind in kinds:
            digest = entry[f"{kind}_spirv_sha256"]
            filename = f"{entry['entry'].removeprefix(PREFIX)}-{index:02d}-{kind}-{digest[:12]}.spv"
            (args.output / filename).write_bytes(checked[index][kind])
            exported.append({
                "entry_index": index, "name": entry["entry"],
                "wave_requirement": entry["wave"], "kind": kind,
                "sha256": digest, "bytes": len(checked[index][kind]),
                "path": filename,
            })
    record = {
        "schema": "ps5-fsr4-compiler-witnesses/1",
        "scope": "offline shader source verification; no PS5 execution",
        "capsule_sha256": manifest["source_overlay_sha256"],
        "manifest_sha256": manifest_hash,
        "verified_payload_count": 2 * len(entries),
        "exported": exported,
    }
    (args.output / "witnesses.json").write_text(json.dumps(record, indent=2) + "\n")
    print(f"Verified {2 * len(entries)} payload hashes; exported {len(exported)} shaders to {args.output}")


if __name__ == "__main__":
    main()
