#!/usr/bin/env python3
# Copyright (C) 2026 BlackBearReloaded
# SPDX-License-Identifier: GPL-3.0-or-later
"""Compile one verified BC250 RC9 FSR4 candidate per shader family, offline only."""
import hashlib
import json
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fsr4_paths import TOOLCHAIN_BIN  # noqa: E402

PREFIX = "fsr4_model_v07_fp8_no_scale_"
ORDER = ["prepass", *(f"pass{i}" for i in range(1, 13)), "postpass"]


def tool(name):
    local = TOOLCHAIN_BIN / name
    found = shutil.which(name) or (str(local) if local.is_file() else None)
    if not found:
        raise RuntimeError(f"Missing SPIRV-Tools program: {name}")
    return found


def run(*args):
    return subprocess.run(args, check=True, text=True, capture_output=True, timeout=180).stdout


def bindings(disassembly):
    decorations, variables, types = {}, {}, {}
    for line in disassembly.splitlines():
        match = re.search(r"OpDecorate %(\S+) (DescriptorSet|Binding) (\d+)", line)
        if match:
            decorations.setdefault(match[1], {})[match[2]] = int(match[3])
        match = re.match(r"\s*%(\S+) = (OpType\S+(?: .*)?|OpVariable %(\S+) \S+)", line)
        if match:
            rhs = match[2].split()
            if rhs[0] == "OpVariable":
                variables[match[1]] = (rhs[1], rhs[2])
            else:
                types[match[1]] = rhs
    result = []
    for ident, (pointer, storage) in variables.items():
        if ident not in decorations:
            continue
        decor = decorations[ident]
        if set(decor) != {"DescriptorSet", "Binding"}:
            raise ValueError(f"Incomplete descriptor decoration: {ident}")
        if storage == "StorageBuffer":
            kind = "buffer"
        elif storage == "UniformConstant":
            definition = types[pointer.lstrip("%")]
            if definition[0] != "OpTypePointer":
                raise ValueError(f"Unexpected descriptor pointer: {ident}")
            element = definition[2].lstrip("%")
            for _ in range(5):
                definition = types[element]
                if definition[0] not in ("OpTypeRuntimeArray", "OpTypeArray"):
                    break
                element = definition[1].lstrip("%")
            if definition[0] == "OpTypeSampler":
                kind = "sampler"
            elif definition[0] == "OpTypeImage":
                if len(definition) < 8 or definition[6] not in ("1", "2"):
                    raise ValueError(f"Unsupported image descriptor: {definition}")
                kind = "storage-image" if definition[6] == "2" else "sampled-image"
            else:
                raise ValueError(f"Unsupported descriptor type: {definition[0]}")
        else:
            raise ValueError(f"Unsupported descriptor storage: {storage}")
        result.append((decor["DescriptorSet"], decor["Binding"], kind,
                       1 if kind == "sampler" else 64))
    if len(result) != len(set((s, b) for s, b, _, _ in result)):
        raise ValueError("Descriptor binding conflict survived remap")
    return sorted(result)


def main():
    source = Path(sys.argv[1] if len(sys.argv) > 1 else "build/fsr4-rc9-all/witnesses.json")
    manifest = json.loads(source.read_text())
    if manifest.get("schema") != "ps5-fsr4-compiler-witnesses/1" or manifest.get("verified_payload_count") != 84:
        raise ValueError("Expected verified 84-module BC250 capsule manifest")
    candidates = {}
    for entry in manifest["exported"]:
        if not entry["name"].startswith(PREFIX):
            continue
        family = entry["name"][len(PREFIX):]
        if family not in ORDER or entry["kind"] != (
                "target" if family == "pass10" else "original"):
            continue
        if family not in candidates or (entry["wave_requirement"] == 32 and
                                       candidates[family]["wave_requirement"] != 32):
            candidates[family] = entry
    if set(candidates) != set(ORDER):
        raise ValueError("Missing FSR4 shader family")
    output = Path("build/fsr4-family-inventory")
    output.mkdir(parents=True, exist_ok=True)
    tools = {name: tool(name) for name in ("spirv-opt", "spirv-val", "spirv-dis")}
    probe = Path("build/fsr4_compile_probe")
    if not probe.is_file():
        raise RuntimeError("Run make fsr4-compile-probe first")
    records = []
    root = source.parent.resolve()
    for family in ORDER:
        entry = candidates[family]
        shader = (source.parent / entry["path"]).resolve()
        if not shader.is_relative_to(root) or hashlib.sha256(shader.read_bytes()).hexdigest() != entry["sha256"]:
            raise ValueError(f"Shader identity mismatch: {family}")
        remapped = output / entry["path"]
        version = struct.unpack_from("<I", shader.read_bytes(), 4)[0]
        environment = "vulkan1.3" if version >= 0x10600 else "vulkan1.2"
        run(tools["spirv-opt"], f"--target-env={environment}",
            "--resolve-binding-conflicts", str(shader), "-o", str(remapped))
        run(tools["spirv-val"], "--target-env", environment, str(remapped))
        layout = bindings(run(tools["spirv-dis"], str(remapped)))
        args = [str(probe), str(remapped),
                *(f"{s}:{b}:{kind}:{count}" for s, b, kind, count in layout)]
        completed = subprocess.run(args, text=True, capture_output=True, timeout=180)
        lines = [json.loads(line) for line in completed.stdout.splitlines() if line.startswith("{")]
        record = {
            "family": family, "kind": entry["kind"],
            "spirv_version": hex(version), "source_sha256": entry["sha256"],
            "remapped_sha256": hashlib.sha256(remapped.read_bytes()).hexdigest(),
            "wave_requirement": entry["wave_requirement"], "bindings": layout,
            "probe_exit_code": completed.returncode, "compiler": lines,
        }
        if completed.returncode:
            record["stderr_tail"] = completed.stderr[-2000:]
        records.append(record)
        print(f"{family}: exit={completed.returncode} "
              f"adapter={lines[-1].get('adapter_result') if lines else 'missing'} "
              f"scratch={lines[0].get('scratch_bytes_per_wave') if lines else 'missing'}",
              flush=True)
    (output / "results.json").write_text(json.dumps({
        "schema": "ps5-fsr4-family-compiler-inventory/1",
        "scope": "host synthetic descriptor ABI; no Vulkan pipeline or GPU execution",
        "source_manifest_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "records": records,
    }, indent=2) + "\n")
    return int(any(r["probe_exit_code"] for r in records))


if __name__ == "__main__":
    sys.exit(main())
