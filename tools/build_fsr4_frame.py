#!/usr/bin/env python3
# Copyright (C) 2026 BlackBearReloaded
# SPDX-License-Identifier: GPL-3.0-or-later
"""Build the native PPSA88900 image-stage/full first-frame replay witness."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
import zlib
from build_fsr4_clear import ROOT, DIST_SDK, build_native_app, native_inputs

CORRECTED_GRAPH = "874530841b8ff2a4b8b62544ff2faf5bd7a16cf166dbc583b1995eddbe261ff7"
ORIGINAL_GRAPH = "0bec7f83e033ff72b151fe7b2fa945244d51474120f90757fc144ca6d2310f9d"
FORMATS = {
    (4, 1, 1): "VK_FORMAT_R32_SFLOAT",
    (4, 2, 1): "VK_FORMAT_R32G32_SFLOAT",
    (4, 4, 1): "VK_FORMAT_R32G32B32A32_SFLOAT",
    (2, 4, 1): "VK_FORMAT_R16G16B16A16_SFLOAT",
    (1, 4, 2): "VK_FORMAT_R8G8B8A8_UNORM",
    (4, 1, 4): "VK_FORMAT_R32_UINT",
}


FRAME_DECLARATIONS = "#include <stdint.h>\n#include <stddef.h>\n"
FRAME_DECLARATIONS += "struct fsr4_resource_desc { const char *asset, *expected; VkFormat format; uint32_t width,height; size_t bytes; uint32_t uniform; VkImageUsageFlags usage; };\n"
FRAME_DECLARATIONS += "struct fsr4_binding { uint32_t set,binding; VkDescriptorType type; uint32_t resource; };\n"
FRAME_DECLARATIONS += "struct fsr4_output { uint32_t resource,crc; };\n"
FRAME_DECLARATIONS += "struct fsr4_pass { uint32_t index,groups[3]; const uint32_t *code; size_t code_bytes; const struct fsr4_binding *bindings; uint32_t binding_count; const struct fsr4_output *outputs; uint32_t output_count; };\n"

def sha(data):
    return hashlib.sha256(data).hexdigest()


def load_capture(path, expected):
    raw = (path / "graph.json").read_bytes()
    receipt = json.loads((path / "complete.json").read_text())
    graph = json.loads(raw)
    count = len(graph["dispatches"])
    if (sha(raw) != expected or receipt["graph_sha256"] != expected or
            receipt["dispatches"] != count or count == 0 or count % 28 or count > 28 * 600):
        raise ValueError("Capture identity mismatch")
    return graph


def blob(path, record):
    digest = record["sha256"]
    if not re.fullmatch("[a-f0-9]{64}", digest):
        raise ValueError("Invalid asset digest")
    data = (path / (digest + ".bin")).read_bytes()
    if sha(data) != digest or len(data) != record["bytes"]:
        raise ValueError("Asset identity mismatch")
    return data



def validate_resource_continuity(dispatches):
    """Reject captures whose omitted copies/clears would change replay state."""
    state = {}
    for dispatch in dispatches:
        writes = {b["descriptor"]["resource"] for b in dispatch["uav"]}
        if writes & {b["descriptor"]["resource"] for b in dispatch["srv"]}:
            raise ValueError("Simultaneous SRV/UAV aliases need a before-event SRV capture")
        for role in ("srv", "uav"):
            for binding in dispatch[role]:
                resource = binding["descriptor"]["resource"]
                before = binding["before_event"] if role == "uav" else binding["at_event"]
                if resource in state and state[resource] != before:
                    raise ValueError("Resource changed between captured dispatches: " + resource)
                state[resource] = binding["at_event"]


def select_dispatches(graph, start_frame, frames, isolated=None):
    count = len(graph["dispatches"])
    if isolated is not None:
        if frames != 1 or start_frame != 0:
            raise ValueError("--frames/--start-frame cannot be combined with an isolated --dispatch")
        if isolated < 0 or isolated >= count or isolated % 28 not in (0, 1, 27):
            raise ValueError("Isolated dispatch is not a captured image stage")
        return [isolated]
    if start_frame < 0 or frames < 1 or (start_frame + frames) * 28 > count:
        raise ValueError("Selected frames exceed the capture")
    return list(range(28 * start_frame, 28 * (start_frame + frames)))


def validate_shader_sequence(graph, baseline):
    """Permit new dimensions/data only with the previously audited shader sequence."""
    sequence = [x["shader"] for x in baseline["dispatches"][:28]]
    for index, dispatch in enumerate(graph["dispatches"]):
        if dispatch["shader"] != sequence[index % 28]:
            raise ValueError("Captured shader sequence changed at pass %d" % index)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", type=Path, default=ROOT / "build/reference-runtime/capture-export-scalar-unpack")
    parser.add_argument("--capture-sha256", default=CORRECTED_GRAPH, help="Expected verified graph digest for an alternate workload")
    parser.add_argument("--original", type=Path, default=ROOT / "build/reference-runtime/capture-export-before01")
    parser.add_argument("--dispatch", type=int, help="An isolated image stage from any captured frame; default is the complete first frame")
    parser.add_argument("--frames", type=int, choices=range(1, 601), default=1, help="Connected captured frames, retaining history")
    parser.add_argument("--start-frame", type=int, choices=range(600), default=0, help="First captured frame; initializes resources from its reference input state")
    parser.add_argument("--fp32-fma", choices=("explicit", "compiler-default"), default="explicit",
                        help="Select the experimental explicit FP32 fusion or the prior compiler behavior")
    parser.add_argument("--out", type=Path, default=ROOT / "build/fsr4-frame-app")
    parser.add_argument("--use-staged-sdk", action="store_true")
    args = parser.parse_args()
    if args.dispatch is not None and (args.frames != 1 or args.start_frame != 0):
        parser.error("--frames/--start-frame cannot be combined with an isolated --dispatch")
    capture, original, out = args.capture.resolve(), args.original.resolve(), args.out.resolve()
    graph, old = load_capture(capture, args.capture_sha256), load_capture(original, ORIGINAL_GRAPH)
    baseline = load_capture(ROOT / "build/reference-runtime/capture-export-scalar-unpack", CORRECTED_GRAPH)
    validate_shader_sequence(graph, baseline)
    try:
        indices = select_dispatches(graph, args.start_frame, args.frames, args.dispatch)
    except ValueError as error:
        parser.error(str(error))
    _, sdk, _, _, _ = native_inputs()
    validate_resource_continuity([graph["dispatches"][i] for i in indices])
    package = out / "PPSA88900"
    for folder in ["assets", "sce_sys", "sce_module"]:
        (package / folder).mkdir(parents=True, exist_ok=True)
    textures = {x["resourceId"]: x for x in graph["textures"]}
    resources, lookup, passes = [], {}, []
    for index in indices:
        dispatch = graph["dispatches"][index]
        bindings, outputs = [], []
        groups = dispatch["dispatchDimension"]
        if len(groups) != 3 or any(type(x) is not int or not 1 <= x <= 65535 for x in groups):
            raise ValueError("Invalid dispatch dimensions")
        for set_index, label in enumerate(["srv", "uav", "cbv", "samplers"]):
            for binding in dispatch[label]:
                reflection = dispatch[label + "_reflection"][binding["access"]["index"]]
                if reflection["fixedBindSetOrSpace"] or reflection["bindArraySize"] != 1 or binding["access"]["arrayElement"]:
                    raise ValueError("Only singleton space-zero descriptors are supported")
                register = reflection["fixedBindNumber"]
                if label == "samplers":
                    sampler = binding["sampler"]
                    if (sampler["addressU"], sampler["addressV"], sampler["addressW"]) != (3, 3, 3) or sampler["filter"] != dict(filter=0, magnify=2, minify=2, mip=2):
                        raise ValueError("Unexpected reference sampler")
                    bindings.append((set_index, register, "VK_DESCRIPTOR_TYPE_SAMPLER", 0))
                    continue
                descriptor = binding["descriptor"]
                rid = descriptor["resource"]
                key = f"constant-{index}-{register}" if label == "cbv" else rid
                if key not in lookup:
                    image = textures.get(rid) if label != "cbv" else None
                    initial = blob(capture, binding["before_event"] if label == "uav" else binding["at_event"])
                    if label == "cbv":
                        # The captured providers declare at most this common constant block.
                        initial = initial[:272]
                    fmt, width, height = "VK_FORMAT_UNDEFINED", 0, 0
                    if image:
                        f = image["format"]
                        fmt = FORMATS[(f["compByteWidth"], f["compCount"], f["compType"])]
                        width, height = image["width"], image["height"]
                        if image["mips"] != 1 or image["arraysize"] != 1 or descriptor["firstMip"] or descriptor["firstSlice"]:
                            raise ValueError("Only one-mip 2D images are supported")
                        if len(initial) != width * height * f["compByteWidth"] * f["compCount"]:
                            raise ValueError("Unexpected packed image footprint")
                    elif label != "cbv" and descriptor["byteOffset"]:
                        raise ValueError("Unexpected nonzero buffer view offset")
                    n = len(resources)
                    lookup[key] = n
                    resources.append(dict(id=key, format=fmt, width=width, height=height,
                                          bytes=len(initial), uniform=label == "cbv",
                                          sampled=False, storage=False, initial=initial, expected=None))
                n = lookup[key]
                resource = resources[n]
                if resource["format"] != "VK_FORMAT_UNDEFINED":
                    resource["sampled"] |= label == "srv"
                    resource["storage"] |= label == "uav"
                    kind = "VK_DESCRIPTOR_TYPE_STORAGE_IMAGE" if label == "uav" else "VK_DESCRIPTOR_TYPE_SAMPLED_IMAGE"
                else:
                    kind = "VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER" if label == "cbv" else "VK_DESCRIPTOR_TYPE_STORAGE_BUFFER"
                bindings.append((set_index, register, kind, n))
                if label == "uav":
                    expected = blob(capture, binding["at_event"])
                    if len(expected) != resource["bytes"]:
                        raise ValueError("Output footprint changed")
                    resource["expected"] = expected
                    outputs.append((n, zlib.crc32(expected)))
        # Execute the original DXIL. Corrected WARP supplies data, not a replacement PS5 shader.
        shader = old["dispatches"][index % 28]["shader"]
        passes.append(dict(index=index, groups=dispatch["dispatchDimension"],
                           shader=blob(original, shader), bindings=bindings, outputs=outputs))
    env = dict(os.environ, PS5_FSR4_FP32_FMA="1" if args.fp32_fma == "explicit" else "0", PS5_PAYLOAD_SDK=str(sdk), PS5VK_SHADER_INT8_DIAGNOSTIC="1",
               PS5VK_SHADER_INT16_DIAGNOSTIC="1", PS5VK_SUBGROUP_ALL_DIAGNOSTIC="1",
               PS5VK_FSR4_STORAGE_DIAGNOSTIC="1")
    if not args.use_staged_sdk:
        subprocess.run([sys.executable, str(ROOT / "tools/build_sdk.py")], env=env, check=True)
    subprocess.run(["make", "fsr4-dxil-converter"], cwd=ROOT, check=True)
    header = FRAME_DECLARATIONS
    header += "static const struct fsr4_resource_desc fsr4_resources[] = {\n"
    for n, resource in enumerate(resources):
        name, expected_name = f"input-{n}.bin", f"expected-{n}.bin" if resource["expected"] is not None else ""
        (package / "assets" / name).write_bytes(resource["initial"])
        if expected_name:
            (package / "assets" / expected_name).write_bytes(resource["expected"])
        flags = ["VK_IMAGE_USAGE_TRANSFER_DST_BIT"]
        if resource["sampled"]:
            flags.append("VK_IMAGE_USAGE_SAMPLED_BIT")
        if resource["storage"]:
            flags += ["VK_IMAGE_USAGE_STORAGE_BIT", "VK_IMAGE_USAGE_TRANSFER_SRC_BIT"]
        header += '{"%s","%s",%s,%d,%d,%d,%d,%s},\n' % (name, expected_name, resource["format"], resource["width"], resource["height"], resource["bytes"], resource["uniform"], "|".join(flags))
    header += "};\n"
    identities = []
    for n, dispatch in enumerate(passes):
        dxil, spv = out / f"pass{dispatch['index']}.dxil", out / f"pass{dispatch['index']}.spv"
        dxil.write_bytes(dispatch["shader"])
        subprocess.run([str(ROOT / "build/fsr4_dxil_to_spirv"), str(dxil), str(spv)], env=env, check=True)
        subprocess.run([str(ROOT / "build/runtime-graphics/toolchain/usr/bin/spirv-val"), "--target-env", "vulkan1.3", str(spv)], check=True)
        code = spv.read_bytes()
        header += f"static const uint32_t code{n}[] = {{" + ",".join(hex(w) for w in struct.unpack(f"<{len(code)//4}I", code)) + "};\n"
        header += f"static const struct fsr4_binding bindings{n}[] = {{" + ",".join("{%d,%d,%s,%d}" % x for x in dispatch["bindings"]) + "};\n"
        header += f"static const struct fsr4_output outputs{n}[] = {{" + ",".join("{%d,0x%08xu}" % x for x in dispatch["outputs"]) + "};\n"
        identities.append(dict(index=dispatch["index"], dxil_sha256=sha(dispatch["shader"]), spirv_sha256=sha(code)))
    header += "static const struct fsr4_pass fsr4_passes[] = {\n"
    for n, dispatch in enumerate(passes):
        header += "{%d,{%s},code%d,sizeof(code%d),bindings%d,%d,outputs%d,%d},\n" % (dispatch["index"], ",".join(map(str, dispatch["groups"])), n, n, n, len(dispatch["bindings"]), n, len(dispatch["outputs"]))
    header += "};\n"
    fixture_id = sha(header.encode() + b"".join(x["initial"] + (x["expected"] or b"") for x in resources))
    header += f'#define FSR4_FIXTURE_ID "{fixture_id}"\n'
    (out / "fsr4_frame_fixture.h").write_text(header)
    build_native_app(out, ROOT / "examples/native_consumer/fsr4_frame_main.c", "FSR4 Native Frame Test")
    manifest = dict(title="PPSA88900", fixture_id=fixture_id, dispatches=identities,
                    translation_policy="scalar FP16 RTZ, typed-resource FP16 RTE, FP32 dot2 products, " + args.fp32_fma + " FP32 FMA, FP16 denorm preserve",
                    fp32_fma=args.fp32_fma,
                    converter_patch_sha256=sha((ROOT / "tools/dxil-spirv-fsr4-fp16.patch").read_bytes()),
                    converter_library_sha256=sha((ROOT / "build/dxil-spirv/libdxil-spirv-c-shared.so").read_bytes()),
                    reference_graph_sha256=args.capture_sha256, original_graph_sha256=ORIGINAL_GRAPH,
                    complete_fsr4=False, hardware_tested=False,
                    frames=args.frames if args.dispatch is None else 0,
                    start_frame=args.start_frame if args.dispatch is None else args.dispatch // 28,
                    full_first_frame=args.dispatch is None and args.frames == 1 and args.start_frame == 0,
                    sdk_archive_sha256=sha((DIST_SDK / "lib/libps5vk.a").read_bytes()),
                    compiler_archive_sha256=sha((DIST_SDK / "lib/libpsbc.a").read_bytes()),
                    resources=[{k:v for k,v in x.items() if k not in ("initial", "expected")} for x in resources],
                    files={str(p.relative_to(package)):sha(p.read_bytes()) for p in sorted(package.rglob("*")) if p.is_file()})
    (out / "artifact.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(dict(package=str(package), fixture_id=fixture_id), indent=2))


if __name__ == "__main__":
    main()
