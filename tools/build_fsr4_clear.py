#!/usr/bin/env python3
# Copyright (C) 2026 BlackBearReloaded
# SPDX-License-Identifier: GPL-3.0-or-later
"""Build PPSA88900 to replay a pinned FSR4 clear or neural pass against a fixture."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import zlib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from build_consumer import native_inputs, DIST_SDK

CLEAR_SHA = "f3b9b2e3dc6eb22d593fa74ec77223cf828c85ba8910be2a2c5ddb5833d7bbed"
NEURAL_SHADERS = {
    3: "d3c654481614c4a3258f8e96bc5d2760021a6d706928cc274a5b594419b52036",
    13: "ab25efdb94d8c408881cc6f9d1e659de41152f486cdea6c579bf8a32ee973990",
    15: "d82b601be75b0ac5377e635e654243bfd9efadce0079fb505522f8e12f381a3b",
    17: "4a9af4fdf3ba7448ef173573ba7a90e61813a69c6fa93f02c54cf20a1627e6c6",
    19: "c4995f0a85034d3c04ede6634391f7eb4f6dfd806b6173bd8bdd4ff986664471",
}
SCRATCH_BYTES = 20880256
CONSTANT_BYTES = 272
MODEL_SHA = "a54e552ff69a3f7199861417f7ae461d1f67f636ab532c6ec9d8ea5d2963735a"
CHAIN_SHADER_ID = "fa575eb0cb663e8dfa46fcbcae22bd91e005f620b4e9379849a9ea5e94a9738f"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def padding_offsets():
    # Four rectangles decoded from the pinned pass0_post DXIL.
    # Tensor 96x72, fixed 962-element physical row, 16 bytes per element.
    return [y * 15392 + x * 16
            for y, xs in [(0, range(102)), (73, range(102)),
                          *[(y, [0, *range(97, 102)]) for y in range(1, 73)]]
            for x in xs]


def fixture():
    before = bytes([0xa5]) * SCRATCH_BYTES
    expected = bytearray(before)
    offsets = padding_offsets()
    if len(offsets) != 636 or len(set(offsets)) != 636:
        raise ValueError("Padding footprint changed")
    for offset in offsets:
        if not 0 <= offset <= SCRATCH_BYTES - 16:
            raise ValueError("Padding outside buffer")
        expected[offset:offset + 16] = bytes(16)
    return before, bytes(expected)



def buffer_chain(graph):
    records = graph["dispatches"][2:27]
    if len(records) != 25 or sha("".join(x["shader"]["sha256"] for x in records).encode()) != CHAIN_SHADER_ID:
        raise ValueError("Not the pinned buffer-only shader sequence")
    previous = None
    for record in records:
        if (len(record["uav"]) != 1 or len(record["srv"]) > 1 or len(record["cbv"]) > 1 or
                record["uav"][0]["descriptor"]["resource"] != "ResourceId::295"):
            raise ValueError("Unexpected buffer-chain resources")
        tensor = record["uav"][0]
        if (tensor["descriptor"]["byteOffset"] != 0 or
                tensor["descriptor"]["byteSize"] != SCRATCH_BYTES or
                tensor["before_event"]["bytes"] != SCRATCH_BYTES or
                tensor["at_event"]["bytes"] != SCRATCH_BYTES):
            raise ValueError("Unexpected tensor view")
        if previous is not None and previous != tensor["before_event"]:
            raise ValueError("Tensor changed between captured dispatches")
        previous = tensor["at_event"]
        if record["srv"]:
            model = record["srv"][0]
            if (model["descriptor"]["resource"] != "ResourceId::296" or
                    model["descriptor"]["byteOffset"] != 0 or
                    model["at_event"] != {"sha256": MODEL_SHA, "bytes": 131072}):
                raise ValueError("Model changed inside buffer chain")
        if len(record["dispatchDimension"]) != 3 or any(
                not isinstance(x, int) or x <= 0 or x > 65535 for x in record["dispatchDimension"]):
            raise ValueError("Invalid dispatch dimensions")
    return records

def build_native_app(out, source_file, title_name, extra_sources=(), include_dirs=()):
    """Use the same native template, heap and public SDK for both witnesses."""
    foundation, sdk, compiler, builder, gears = native_inputs()
    env = dict(os.environ, PS5_PAYLOAD_SDK=str(sdk))
    package = out / "PPSA88900"
    for folder in ("sce_sys", "sce_module"):
        (package / folder).mkdir(parents=True, exist_ok=True)
    objects, crt = [], out / "crt.o"
    includes = ["-I" + str(DIST_SDK / "include"), "-I" + str(out), *("-I" + str(x) for x in include_dirs)]
    for n, source in enumerate((source_file, *extra_sources)):
        obj, dep = out / ("main.o" if n == 0 else f"extra{n}.o"), out / ("main.d" if n == 0 else f"extra{n}.d")
        subprocess.run([str(compiler), "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
                        "-MD", "-MF", str(dep), *includes, "-c", str(source), "-o", str(obj)],
                       env=env, check=True)
        # Only public SDK, SDK-level FSR4 runtime and generated headers are used by this consumer.
        deps = dep.read_text()
        if str(ROOT / "src/ps5vk_") in deps or str(ROOT / "native") + "/" in deps:
            raise ValueError("Private implementation header in clear consumer")
        objects.append(obj)
    subprocess.run([str(sdk / "bin/prospero-clang++"), "-std=c++20", "-O2",
                    "-fno-exceptions", "-fno-rtti", "-c",
                    str(foundation / "tooling/native/app_crt.cpp"), "-o", str(crt)], env=env, check=True)
    heap = out / "native_heap.o"
    subprocess.run([str(compiler), "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
                    "-c", str(ROOT / "examples/native_consumer/native_heap.c"), "-o", str(heap)],
                   env=env, check=True)
    pie, elf = out / "clear-pie.elf", out / "eboot.elf"
    subprocess.run([str(sdk / "bin/prospero-lld"), "-L" + str(sdk / "target/lib"),
                    "-T", str(DIST_SDK / "lib/ps5-pie.ld"), "--eh-frame-hdr",
                    "--version-script", str(DIST_SDK / "lib/app-symbols.map"),
                    "-e", "_start", "-o", str(pie), str(crt), *map(str, objects), str(heap),
                    *["--wrap=" + name for name in ("malloc", "calloc", "realloc", "free",
                      "posix_memalign", "memalign", "aligned_alloc", "malloc_usable_size")],
                    str(DIST_SDK / "lib/libps5vk.a"), str(DIST_SDK / "lib/libpsbc.a"),
                    *[str(sdk / "target/lib" / n) for n in
                      ("libc++.a", "libc++abi.a", "libunwind.a", "libc.a")],
                    "--as-needed", str(sdk / "target/lib/libkernel.so"),
                    *sorted(str(x) for x in (sdk / "target/lib").glob("*.so")),
                    str(DIST_SDK / "lib/libSceAgc.so"), str(DIST_SDK / "lib/libSceAgcDriver.so")],
                   check=True)
    subprocess.run([str(builder), "link", "--in", str(pie), "--out", str(elf),
                    "--stub-dir", str(sdk / "target/lib"), "--module-sdk", "0x02000009",
                    "--stub", str(DIST_SDK / "lib/libSceAgc.so"),
                    "--stub", str(DIST_SDK / "lib/libSceAgcDriver.so"),
                    "--companion-sdk", "0x08050001", "--file-name", "eboot.elf"], check=True)
    subprocess.run([str(builder), "self", "--sign", "--in", str(elf), "--out",
                    str(package / "eboot.bin"), "--magic", "0x1D3D154F"], check=True)
    param = json.loads((gears / "sce_sys/param.json").read_text())
    param.update(titleId="PPSA88900", conceptId="88900",
                 contentId="UP9000-PPSA88900_00-FSR4CLEARTEST001")
    param["localizedParameters"]["en-US"]["titleName"] = title_name
    (package / "sce_sys/param.json").write_text(json.dumps(param, indent=2) + "\n")
    shutil.copyfile(foundation / "runtime/libc.prx", package / "sce_module/libc.prx")
    # The shell refuses to launch PPSA88900 (0x80940033) without the launch assets.
    for name in ("icon0.png", "pic0.dds", "pic1.dds", "snd0.at9"):
        shutil.copyfile(foundation / "sce_sys" / name, package / "sce_sys" / name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=ROOT / "build/fsr4-clear-app")
    parser.add_argument("--use-staged-sdk", action="store_true")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dispatch", type=int, choices=[2, 3, 13, 15, 17, 19], default=2)
    mode.add_argument("--chain", action="store_true", help="Replay connected buffer passes 2 through 26")
    args = parser.parse_args()
    source, out = args.capture.resolve(), args.out.resolve()
    receipt = json.loads((source / "complete.json").read_text())
    graph_data = (source / "graph.json").read_bytes()
    if sha(graph_data) != receipt["graph_sha256"] or receipt["dispatches"] != 112:
        raise ValueError("Capture export receipt mismatch")
    graph = json.loads(graph_data)
    clear = graph["dispatches"][args.dispatch]
    if not args.chain and args.dispatch == 2 and (clear["shader"]["sha256"] != CLEAR_SHA or clear["dispatchDimension"] != [20, 1, 1]
            or clear["threads"] != [32, 1, 1]):
        raise ValueError("Not the pinned first padding-clear dispatch")

    def read_blob(record):
        digest = record["sha256"]
        if not re.fullmatch("[0-9a-f]{64}", digest):
            raise ValueError("Invalid blob identity")
        data = (source / (digest + ".bin")).read_bytes()
        if sha(data) != digest or len(data) != record["bytes"]:
            raise ValueError("Capture blob mismatch")
        return data

    if args.chain:
        records = buffer_chain(graph)
        indices = list(range(2, 27))
        before = read_blob(records[0]["uav"][0]["before_event"])
        expected = read_blob(records[-1]["uav"][0]["at_event"])
        model = read_blob(next(x["srv"][0]["at_event"] for x in records if x["srv"]))
        constants = b"".join(read_blob(x["cbv"][0]["at_event"])[:CONSTANT_BYTES]
                             if x["cbv"] else bytes(CONSTANT_BYTES) for x in records)
        if len(constants) != CONSTANT_BYTES * len(records) or before == expected:
            raise ValueError("Invalid connected fixture")
        oracle = "Captured WARP buffer chain; one initial tensor upload, no intermediate restoration"
    else:
        records, indices = [clear], [args.dispatch]
        constants = (read_blob(clear["cbv"][0]["at_event"])[:CONSTANT_BYTES]
                     if clear["cbv"] else bytes(CONSTANT_BYTES))
        model = b""
        if args.dispatch == 2:
            if len(constants) != CONSTANT_BYTES or struct.unpack_from("<2I", constants) != (96, 72):
                raise ValueError("Unexpected tensor constants")
            before, expected = fixture()
            oracle = "CPU rectangles decoded from pinned DXIL; captured tensor dimensions"
        else:
            if (clear["shader"]["sha256"] != NEURAL_SHADERS[args.dispatch] or
                len(clear["srv"]) != 1 or len(clear["uav"]) != 1 or
                clear["srv"][0]["descriptor"]["resource"] != "ResourceId::296" or
                clear["uav"][0]["descriptor"]["resource"] != "ResourceId::295" or
                clear["dispatchDimension"] != ([2,72,1] if args.dispatch == 3 else [1,18,1]) or clear["threads"] != [64,1,1]):
                raise ValueError("Unexpected neural pass resource graph")
            before = read_blob(clear["uav"][0]["before_event"])
            expected = read_blob(clear["uav"][0]["at_event"])
            model = read_blob(clear["srv"][0]["at_event"])
            if (len(before) != SCRATCH_BYTES or len(expected) != SCRATCH_BYTES or before == expected or
                sha(model) != MODEL_SHA):
                raise ValueError("Invalid neural pass fixture")
            oracle = "Captured WARP output; input immediately before the dispatch"

    foundation, sdk, compiler, builder, gears = native_inputs()
    env = dict(os.environ, PS5_PAYLOAD_SDK=str(sdk))
    if model:
        env.update(PS5VK_SHADER_INT8_DIAGNOSTIC="1", PS5VK_SHADER_INT16_DIAGNOSTIC="1",
                   PS5VK_SUBGROUP_ALL_DIAGNOSTIC="1")
    if not args.use_staged_sdk or not (DIST_SDK / "lib/libps5vk.a").is_file():
        subprocess.run([sys.executable, str(ROOT / "tools/build_sdk.py")], env=env, check=True)
    package = out / "PPSA88900"
    for folder in ("sce_sys", "sce_module", "assets"):
        (package / folder).mkdir(parents=True, exist_ok=True)
    subprocess.run(["make", "fsr4-dxil-converter"], cwd=ROOT, check=True)
    programs, identities = [], []
    for index, record in zip(indices, records):
        stem = "clear" if len(records) == 1 else f"pass{index}"
        shader = read_blob(record["shader"])
        dxil, spv = out / (stem + ".dxil"), out / (stem + ".spv")
        dxil.write_bytes(shader)
        subprocess.run([str(ROOT / "build/fsr4_dxil_to_spirv"), str(dxil), str(spv)], check=True)
        subprocess.run([str(ROOT / "build/runtime-graphics/toolchain/usr/bin/spirv-val"),
                        "--target-env", "vulkan1.3", str(spv)], check=True)
        programs.append(spv.read_bytes())
        identities.append(dict(index=index, groups=record["dispatchDimension"],
                               dxil_sha256=sha(shader), spirv_sha256=sha(programs[-1]),
                               expected_crc32=zlib.crc32(read_blob(record["uav"][0]["at_event"])
                                                       if args.chain else expected)))
    identity_bytes = json.dumps(identities, sort_keys=True).encode() if args.chain else b""
    fixture_id = sha(b"".join(programs) + constants + before + expected + model + identity_bytes)
    header = "#include <stdint.h>\n#include <stddef.h>\n"
    header += '#define FSR4_FIXTURE_ID "' + fixture_id + '"\n'
    header += "#define FSR4_SCRATCH_BYTES %du\n" % SCRATCH_BYTES
    header += "#define FSR4_CONSTANT_BYTES %du\n" % CONSTANT_BYTES
    header += "#define FSR4_MODEL_BYTES %du\n" % len(model)
    header += "#define FSR4_DISPATCH_COUNT %du\n" % len(records)
    for n, program in enumerate(programs):
        words = struct.unpack("<%dI" % (len(program) // 4), program)
        header += "static const uint32_t fsr4_spirv_%d[] = {" % n + ",".join(hex(w) for w in words) + "};\n"
    header += "struct fsr4_dispatch { uint32_t index, groups[3], expected_crc32; const uint32_t *code; size_t code_bytes; };\n"
    header += "static const struct fsr4_dispatch fsr4_dispatches[] = {\n"
    for n, identity in enumerate(identities):
        header += "{%du,{%s},0x%08xu,fsr4_spirv_%d,sizeof(fsr4_spirv_%d)},\n" % (
            identity["index"], ",".join(map(str, identity["groups"])), identity["expected_crc32"], n, n)
    header += "};\n"
    (out / "fsr4_clear_fixture.h").write_text(header)
    for name, data in [("before.bin", before), ("expected.bin", expected), ("constants.bin", constants), ("model.bin", model)]:
        (package / "assets" / name).write_bytes(data)
    build_native_app(out, ROOT / "examples/native_consumer/fsr4_clear_main.c",
                     "FSR4 Captured Kernel Test")
    manifest = dict(title="PPSA88900",
                    translation_policy="scalar FP16 RTZ, typed-resource FP16 RTE, FP32 dot2 products, explicit FP32 FMA, FP16 denorm preserve",
                    converter_patch_sha256=sha((ROOT / "tools/dxil-spirv-fsr4-fp16.patch").read_bytes()),
                    converter_library_sha256=sha((ROOT / "build/dxil-spirv/libdxil-spirv-c-shared.so").read_bytes()),
                    sdk_archive_sha256=sha((DIST_SDK / "lib/libps5vk.a").read_bytes()),
                    compiler_archive_sha256=sha((DIST_SDK / "lib/libpsbc.a").read_bytes()),
                    requested_diagnostics=bool(model), fixture_id=fixture_id,
                    dispatch_index=None if args.chain else args.dispatch, dispatches=identities,
                    shader_dxil_sha256=None if args.chain else identities[0]["dxil_sha256"],
                    shader_spirv_sha256=None if args.chain else identities[0]["spirv_sha256"],
                    expected_sha256=sha(expected),
                    changed_bytes=sum(a != b for a,b in zip(before,expected)), complete_fsr4=False, hardware_tested=False,
                    oracle=oracle,
                    files={str(x.relative_to(package)):sha(x.read_bytes())
                           for x in sorted(package.rglob("*")) if x.is_file()})
    (out / "artifact.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(dict(package=str(package), fixture_id=fixture_id), indent=2))


if __name__ == "__main__":
    main()
