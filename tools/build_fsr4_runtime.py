#!/usr/bin/env python3
# Copyright (C) 2026 BlackBearReloaded
# SPDX-License-Identifier: GPL-3.0-or-later
"""Generate the resolution-independent FSR4 runtime tables for src/.

Inputs are the verified local reference exports: the original RC11 export supplies
the DXIL executed natively; the corrected export supplies only the model
initializers. The output header contains AMD-derived shaders and model data and
is therefore written to the ignored build tree, never committed.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from fsr4_paths import TOOLCHAIN_BIN  # noqa: E402
import fsr4_int8_kernels  # noqa: E402
from build_fsr4_frame import blob, load_capture  # noqa: E402

PASSES = 28
# CsTensorSizes rows as pyramid levels (row k holds output / 2^level, rounded up to 8 first).
FSR4_TENSOR_LEVEL = (1, 0, 1, 1, 2, 2, 2, 3, 3, 3, 2, 2, 1, 1, 0, 0, 0)
NAMED_ROLES = {
    "FSR4UPSCALER_SpdAtomicCounter": "SPD_COUNTER",
    "FSR4UPSCALER_Luma_Mip_5": "LUMA_MIP5",
    "FSR4UPSCALER_AutoExposure": "AUTO_EXPOSURE",
    "FSR4UPSCALER_InitializerBuffer": "WEIGHTS",
    "FSR4UPSCALER_History": "HISTORY",
    "FSR4UPSCALER_Recurrent": "RECURRENT",
    "FSR4UPSCALER_ScratchBuffer": "SCRATCH",
    "FSR4UPSCALER_HistoryReprojected": "HISTORY_REPROJECTED",
    "FSR4UPSCALER_RcasIntermediary": "RCAS_INPUT",
}
# Application resources, identified by format and resolution in the pinned workload.
APPLICATION_ROLES = {  # (components, component bytes, at output resolution, written)
    (4, 4, False, False): "COLOR",
    (1, 4, False, False): "DEPTH",
    (2, 4, False, False): "MOTION_VECTORS",
    (4, 4, True, True): "OUTPUT",
}
DESCRIPTOR_TYPES = {"srv_image": "SAMPLED_IMAGE", "uav_image": "STORAGE_IMAGE",
                    "srv_buffer": "STORAGE_BUFFER", "uav_buffer": "STORAGE_BUFFER",
                    "cbv": "UNIFORM_BUFFER", "samplers": "SAMPLER"}


DXC = ROOT / "build/reference-runtime/dxc/linux_dxc_2026_07_29.x86_x64"
# Padding-clear passes: one CsTensorSizes row and the allocated tensor extent (+1) are
# compiled into the shader; right padding is capped at 5 columns, bottom at 1 row.
PADDING = re.compile(r"cbufferLoadLegacy\.i32\(i32 59, %dx\.types\.Handle %\d+, i32 (\d+)\)[^\n]*\n"
                     r"(?:[^\n]*\n){2}\s*%\d+ = sub i32 (\d+), %\d+\n\s*%\d+ = sub i32 (\d+), %\d+\n"
                     r"[^\n]*i32 5\)[^\n]*UMin\(a,b\)\n[^\n]*i32 1\)[^\n]*UMin\(a,b\)")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def padding_elements(width, height, limit_width, limit_height):
    right = min(limit_width - width, 5)
    bottom = min(limit_height - height, 1)
    top = width + 1 + right
    return top + height + height * right + top * bottom


def tensor_extent(output, level):
    """Network tensors: the output rounded up to 8, halved per pyramid level."""
    return tuple(((x + 7) // 8 * 8) >> level for x in output)


def group_rule(index, dispatch, dxil, output):
    """Classify how a pass sizes its dispatch and verify it against the capture."""
    groups = dispatch["dispatchDimension"]
    if index == 0:
        return "SPD", 0, 0, 0
    if index == 1:
        return "PREPASS", 0, 0, 0
    if index == PASSES - 1:
        return "POSTPASS", 0, 0, 0
    ir = subprocess.run([str(DXC / "bin/dxc"), "-dumpbin", str(dxil)], capture_output=True, text=True,
                        env=dict(os.environ, LD_LIBRARY_PATH=str(DXC / "lib")), check=True).stdout
    match = PADDING.search(ir)
    if match:
        row, limit_width, limit_height = map(int, match.groups())
        level = FSR4_TENSOR_LEVEL[row]
        width, height = tensor_extent(output, level)
        expected = [-(-padding_elements(width, height, limit_width, limit_height) // 32), 1, 1]
        if dispatch["threads"] != [32, 1, 1] or groups != expected:
            raise ValueError("Padding rule does not reproduce pass %d: %s vs %s" % (index, groups, expected))
        return "PADDING", row, limit_width, limit_height
    for level in range(4):
        width, height = tensor_extent(output, level)
        if groups == [-(-width // 64), height, 1]:
            return "NETWORK", level, 0, 0
    raise ValueError("No dispatch rule reproduces pass %d: %s" % (index, groups))


def constants_kind(index, dispatch):
    names = [x["name"] for x in dispatch["cbv_reflection"]]
    if not names:
        return "NONE"
    if len(names) != 1:
        raise ValueError("Pass %d declares %d constant blocks" % (index, len(names)))
    if names[0] == "AutoExposureSPDConstants":
        return "SPD"
    return "MLSR" if index in (1, PASSES - 1) else "TENSOR"


def textures_of(graph):
    return {t["resourceId"]: t for t in graph["textures"]}


def frame_length(graph):
    """Dispatches per frame: the model passes, plus RCAS when sharpening is on."""
    dispatches = graph["dispatches"]
    return PASSES + 1 if len(dispatches) > PASSES and "rcas" in dispatches[PASSES]["entryPoint"] else PASSES


def roles(graph):
    names = {r["resourceId"]: r["name"] for r in graph["resources"]}
    textures = {t["resourceId"]: t for t in graph["textures"]}
    first = graph["dispatches"][:frame_length(graph)]
    output = max((t["width"] * t["height"], rid) for rid, t in textures.items())[0]
    written = {u["descriptor"]["resource"] for d in first for u in d["uav"]}
    result = {}
    for rid, name in names.items():
        if name in NAMED_ROLES:
            result[rid] = NAMED_ROLES[name]
        elif name == "FFX_DX12_DynamicRingBuffer":
            result[rid] = "CONSTANTS"
        elif rid in textures:
            t = textures[rid]
            key = (t["format"]["compCount"], t["format"]["compByteWidth"],
                   t["width"] * t["height"] == output, rid in written)
            if key in APPLICATION_ROLES:
                result[rid] = APPLICATION_ROLES[key]
    return result


def pass_bindings(capture, dispatch, role, index):
    """Descriptor bindings of one dispatch, and the model initializers it reads (if any)."""
    bindings, weights = [], None
    for set_index, label in enumerate(("srv", "uav", "cbv", "samplers")):
        for binding in dispatch[label]:
            reflection = dispatch[label + "_reflection"][binding["access"]["index"]]
            if reflection["fixedBindSetOrSpace"] or reflection["bindArraySize"] != 1:
                raise ValueError("Only singleton space-zero descriptors are supported")
            register = reflection["fixedBindNumber"]
            if label == "samplers":
                bindings.append((set_index, register, "SAMPLER", "SAMPLER"))
                continue
            rid = binding["descriptor"]["resource"]
            if rid not in role:
                raise ValueError("Unclassified resource %s at pass %d" % (rid, index))
            r = role[rid]
            if label == "cbv":
                kind = "UNIFORM_BUFFER"
            elif r in ("WEIGHTS", "SCRATCH"):
                kind = "STORAGE_BUFFER"
            else:
                kind = "STORAGE_IMAGE" if label == "uav" else "SAMPLED_IMAGE"
            bindings.append((set_index, register, kind, r))
            if r == "WEIGHTS":
                weights = blob(capture, binding["at_event"])
    return bindings, weights


def convert(capture, record, index, out, env):
    """DXIL of one captured dispatch to validated SPIR-V; returns the DXIL path and SPIR-V bytes."""
    dxil, spv = out / f"pass{index}.dxil", out / f"pass{index}.spv"
    dxil.write_bytes(blob(capture, record))
    subprocess.run([str(ROOT / "build/fsr4_dxil_to_spirv"), str(dxil), str(spv)], env=env, check=True)
    subprocess.run([str(TOOLCHAIN_BIN / "spirv-val"),
                    "--target-env", "vulkan1.3", str(spv)], check=True)
    return dxil, spv.read_bytes()


OP_CAPABILITY, OP_TYPE_BOOL, OP_CONSTANT_TRUE, OP_CONSTANT_FALSE, OP_CONSTANT = 17, 20, 41, 42, 43
OP_FUNCTION, OP_LOAD, OP_ACCESS_CHAIN, OP_DECORATE, OP_COMPOSITE_EXTRACT = 54, 61, 65, 71, 81
OP_COPY_OBJECT, OP_IEQUAL, OP_INOTEQUAL, OP_BRANCH_CONDITIONAL, OP_GROUP_ALL = 83, 170, 171, 250, 334
CAPABILITY_GROUP_NON_UNIFORM, CAPABILITY_GROUP_NON_UNIFORM_VOTE = 61, 62
DECORATION_BUILTIN = 11
BUILTIN_WORKGROUP_ID, BUILTIN_LOCAL_INVOCATION_ID, BUILTIN_GLOBAL_INVOCATION_ID = 26, 27, 28
# Dead-code removal once branches are constant. Inlining lets the aggressive pass drop the
# guard's comparison loop (it keeps function calls); the interface pass then frees the
# built-ins that dead code read, so the second aggressive pass and the trim remove them and
# their capabilities. Instruction simplification is left out: it rewrites live code.
STRIP_PASSES = ("--eliminate-dead-branches", "--inline-entry-points-exhaustive", "--eliminate-dead-functions",
                "--eliminate-dead-code-aggressive", "--remove-unused-interface-variables",
                "--eliminate-dead-code-aggressive", "--trim-capabilities")


def instructions(words):
    i = 5
    while i < len(words):
        yield i, words[i] & 0xffff, words[i] >> 16
        i += words[i] >> 16


def bool_constants(words, values):
    """Ids of the boolean constants for `values`, declared before the first function when missing."""
    bool_type = first_function = None
    found = {}
    for i, op, count in instructions(words):
        if op == OP_TYPE_BOOL:
            bool_type = words[i + 1]
        elif op in (OP_CONSTANT_TRUE, OP_CONSTANT_FALSE) and words[i + 1] == bool_type:
            found.setdefault(op == OP_CONSTANT_TRUE, words[i + 2])
        elif op == OP_FUNCTION and first_function is None:
            first_function = i
    insert = []
    for value in sorted(set(values) - set(found)):
        found[value], words[3] = words[3], words[3] + 1
        insert += [(3 << 16) | (OP_CONSTANT_TRUE if value else OP_CONSTANT_FALSE), bool_type, found[value]]
    return words[:first_function] + insert + words[first_function:], found


def branch_on_constants(code, words, conditions, replace_votes=False):
    """Every OpBranchConditional on a key of `conditions` branches on that constant instead;
    with `replace_votes`, each OpGroupNonUniformAll also becomes a copy of true."""
    if not conditions:
        return code
    words, constants = bool_constants(words, list(conditions.values()) + [True] * replace_votes)
    result = words[:5]
    for i, op, count in instructions(words):
        if replace_votes and op == OP_GROUP_ALL:
            result += [(4 << 16) | OP_COPY_OBJECT, words[i + 1], words[i + 2], constants[True]]
        elif op == OP_BRANCH_CONDITIONAL and words[i + 1] in conditions:
            result += [words[i], constants[conditions[words[i + 1]]]] + words[i + 2:i + count]
        else:
            result += words[i:i + count]
    return struct.pack(f"<{len(result)}I", *result)


def specialize_model_guard(code):
    """Replace the matching-model wave vote with true.

    Passes 3, 6, 7, 8, 9 and 11 compare their model interval with the model
    their fast path was baked from, then vote (OpGroupNonUniformAll) between
    that path and a generic fallback. The runtime embeds exactly that model, so
    the vote is always true: the branch it steers takes the true constant and
    the vote becomes a copy of it, which lets dead-code removal drop the
    comparison loads and the fallback body. Returns the code unchanged when it
    has no vote.
    """
    words = list(struct.unpack(f"<{len(code) // 4}I", code))
    votes = {words[i + 2]: True for i, op, _ in instructions(words) if op == OP_GROUP_ALL}
    return branch_on_constants(code, words, votes, replace_votes=True)


def specialize_single_layer(code):
    """Branch on z == 0 as a constant.

    The runtime dispatches every pass as one z layer of workgroups one invocation
    deep, so the z component of the workgroup and invocation ids is always zero.
    Passes 3, 6, 7, 8, 9 and 11 still choose their baked path with z == 0 and keep
    a generic body for the other layers; that body is dead here. Returns the code
    unchanged when no branch tests z against zero.
    """
    words = list(struct.unpack(f"<{len(code) // 4}I", code))
    ids = (BUILTIN_WORKGROUP_ID, BUILTIN_LOCAL_INVOCATION_ID, BUILTIN_GLOBAL_INVOCATION_ID)
    builtins, values, chains, vectors, z, conditions = set(), {}, set(), set(), set(), {}
    for i, op, count in instructions(words):
        if op == OP_DECORATE and count == 4 and words[i + 2] == DECORATION_BUILTIN and words[i + 3] in ids:
            builtins.add(words[i + 1])
        elif op == OP_CONSTANT and count == 4:
            values[words[i + 2]] = words[i + 3]
        elif op == OP_ACCESS_CHAIN and count == 5 and words[i + 3] in builtins and values.get(words[i + 4]) == 2:
            chains.add(words[i + 2])
        elif op == OP_LOAD and count >= 4 and words[i + 3] in chains:
            z.add(words[i + 2])
        elif op == OP_LOAD and count >= 4 and words[i + 3] in builtins:
            vectors.add(words[i + 2])
        elif op == OP_COMPOSITE_EXTRACT and count == 5 and words[i + 3] in vectors and words[i + 4] == 2:
            z.add(words[i + 2])
        elif op in (OP_IEQUAL, OP_INOTEQUAL) and count == 5:
            a, b = words[i + 3], words[i + 4]
            if (a in z and values.get(b) == 0) or (b in z and values.get(a) == 0):
                conditions[words[i + 2]] = op == OP_IEQUAL
    return branch_on_constants(code, words, conditions)


def capabilities(code):
    words = struct.unpack(f"<{len(code) // 4}I", code)
    found, i = set(), 5
    while i < len(words):
        if words[i] & 0xffff == OP_CAPABILITY:
            found.add(words[i + 1])
        i += words[i] >> 16
    return found


def strip_dead_code(spv, guard_removed):
    """Remove the code constant branches left dead, down to unused built-ins and capabilities.

    A removed guard's comparison loop reads the subgroup built-ins, so without this
    the module still declares GroupNonUniform and GroupNonUniformVote with no
    subgroup operation, which ps5vk refuses at vkCreateShaderModule.
    """
    subprocess.run([str(TOOLCHAIN_BIN / "spirv-opt"), "--target-env=vulkan1.3", *STRIP_PASSES,
                    str(spv), "-o", str(spv)], check=True)
    subprocess.run([str(TOOLCHAIN_BIN / "spirv-val"), "--target-env", "vulkan1.3", str(spv)], check=True)
    code = spv.read_bytes()
    left = capabilities(code) & {CAPABILITY_GROUP_NON_UNIFORM, CAPABILITY_GROUP_NON_UNIFORM_VOTE}
    if guard_removed and left:
        raise SystemExit(f"{spv.name}: subgroup capabilities {sorted(left)} remain after removing the model guard")
    return code


def generate_kernel(index, model, bindings, out):
    """Packed-i16 kernel for dispatch `index`, compiled to validated SPIR-V; returns its words."""
    source, groups = fsr4_int8_kernels.generate(index, model, bindings)
    glsl, spv = out / f"pass{index}.comp", out / f"pass{index}.spv"
    glsl.write_text(source)
    subprocess.run([str(TOOLCHAIN_BIN / "glslangValidator"), "--target-env", "vulkan1.3", "-o", str(spv), str(glsl)],
                   check=True, stdout=subprocess.DEVNULL)
    subprocess.run([str(TOOLCHAIN_BIN / "spirv-val"), "--target-env", "vulkan1.3", str(spv)], check=True)
    print(f"pass {index}: generated packed-i16 kernel, {groups} i16 groups", file=sys.stderr)
    return spv.read_bytes()


def emit_code(header, index, code, bindings):
    words = struct.unpack(f"<{len(code) // 4}I", code)
    header.append(f"static const uint32_t fsr4_code{index}[] = {{" + ",".join(hex(w) for w in words) + "};")
    header.append(f"static const struct fsr4_binding fsr4_bindings{index}[] = {{" + ",".join(
        "{%d,%d,FSR4_DESCRIPTOR_%s,FSR4_ROLE_%s}" % b for b in bindings) + "};")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", type=Path, default=ROOT / "build/reference-runtime/capture-export-scalar-unpack")
    parser.add_argument("--original", type=Path, default=ROOT / "build/reference-runtime/capture-export-before01")
    parser.add_argument("--rcas", type=Path, default=ROOT / "build/reference-runtime/capture-export-rcas",
                        help="Export whose frames end with RCAS sharpening (optional)")
    parser.add_argument("--out", type=Path, default=ROOT / "build/fsr4-runtime")
    parser.add_argument("--fp32-fma", choices=("explicit", "compiler-default"), default="explicit")
    parser.add_argument("--keep-model-guards", action="store_true",
                        help="Keep the runtime model comparison and fallback in passes 3, 6-9 and 11")
    parser.add_argument("--int8-kernels", default="",
                        help="Comma-separated dispatch indices whose converted shader is replaced by a "
                             f"generated packed-i16 kernel (available: {sorted(fsr4_int8_kernels.PASSES)})")
    args = parser.parse_args()
    generated = {int(i) for i in args.int8_kernels.split(",") if i}
    if generated - set(fsr4_int8_kernels.PASSES):
        raise SystemExit(f"no generated kernel for dispatch {sorted(generated - set(fsr4_int8_kernels.PASSES))}")
    capture, original = args.capture.resolve(), args.original.resolve()
    graph = load_capture(capture, json.loads((capture / "complete.json").read_text())["graph_sha256"])
    old = load_capture(original, json.loads((original / "complete.json").read_text())["graph_sha256"])
    role = roles(graph)
    textures = {t["resourceId"]: t for t in graph["textures"]}
    output_id = next(rid for rid, r in role.items() if r == "OUTPUT")
    output = (textures[output_id]["width"], textures[output_id]["height"])
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    subprocess.run(["make", "fsr4-dxil-converter"], cwd=ROOT, check=True, stdout=subprocess.DEVNULL)
    env = dict(os.environ, PS5_FSR4_FP32_FMA="1" if args.fp32_fma == "explicit" else "0")
    header = ["/* Generated by tools/build_fsr4_runtime.py; contains AMD-derived shaders and model data. */",
              "#include <stdint.h>", '#include "fsr4_tables.h"', ""]
    passes, identities, weights = [], [], None
    for index in range(PASSES):
        dispatch, source = graph["dispatches"][index], old["dispatches"][index]
        if dispatch["shader"] != source["shader"] and dispatch["entryPoint"] != source["entryPoint"]:
            raise ValueError("Capture and original disagree at pass %d" % index)
        for frame in range(1, len(old["dispatches"]) // PASSES):
            if old["dispatches"][frame * PASSES + index]["shader"] != source["shader"]:
                raise ValueError("Shader sequence changes between frames at pass %d" % index)
        bindings, data = pass_bindings(capture, dispatch, role, index)
        if data is not None:
            if weights is not None and weights != data:
                raise ValueError("Model initializers differ between passes")
            weights = data
        dxil, code = convert(original, source["shader"], index, out, env)
        specialized = code if args.keep_model_guards else specialize_model_guard(code)
        guard_removed = specialized != code
        specialized = specialize_single_layer(specialized)
        if specialized != code:
            spv = out / f"pass{index}.spv"
            spv.write_bytes(specialized)
            code = strip_dead_code(spv, guard_removed)
        if index in generated:
            if weights is None:
                raise ValueError("Model initializers are needed before pass %d" % index)
            code = generate_kernel(index, weights, bindings, out)
        emit_code(header, index, code, bindings)
        rule = group_rule(index, dispatch, dxil, output)
        passes.append((index, constants_kind(index, dispatch), rule, len(bindings)))
        identities.append(dict(index=index, entry=dispatch["entryPoint"], dxil_sha256=sha(dxil.read_bytes()),
                               spirv_sha256=sha(code), constants=passes[-1][1], bindings=bindings,
                               groups=rule, reference_groups=dispatch["dispatchDimension"]))
    if weights is None:
        raise ValueError("Model initializers were not captured")
    header.append("static const struct fsr4_pass_info fsr4_passes[FSR4_PASS_COUNT] = {")
    for index, kind, (rule, tensor, limit_width, limit_height), count in passes:
        header.append("{fsr4_code%d,sizeof(fsr4_code%d),FSR4_CONSTANTS_%s,%d,fsr4_bindings%d,"
                      "FSR4_GROUPS_%s,%d,%d,%d}," % (index, index, kind, count, index, rule, tensor,
                                                    limit_width, limit_height))
    header.append("};")
    rcas = args.rcas.resolve() if args.rcas else None
    if rcas and (rcas / "complete.json").is_file():
        sharpened = load_capture(rcas, json.loads((rcas / "complete.json").read_text())["graph_sha256"])
        if frame_length(sharpened) != PASSES + 1:
            raise ValueError("The sharpening export has no RCAS pass")
        if sharpened["dispatches"][PASSES - 1]["shader"] != graph["dispatches"][PASSES - 1]["shader"]:
            raise ValueError("The sharpening export runs a different postpass")
        dispatch = sharpened["dispatches"][PASSES]
        if [x["name"] for x in dispatch["cbv_reflection"]] != ["cbRCAS"]:
            raise ValueError("Unexpected RCAS constants")
        rcas_role = roles(sharpened)
        bindings, _ = pass_bindings(rcas, dispatch, rcas_role, PASSES)
        rcas_output = next(rid for rid, r in rcas_role.items() if r == "OUTPUT")
        size = (textures_of(sharpened)[rcas_output]["width"], textures_of(sharpened)[rcas_output]["height"])
        if dispatch["dispatchDimension"] != [-(-size[0] // 16), -(-size[1] // 16), 1]:
            raise ValueError("RCAS dispatch rule does not reproduce the capture")
        dxil, code = convert(rcas, dispatch["shader"], PASSES, out, env)
        emit_code(header, PASSES, code, bindings)
        header.append("#define FSR4_HAS_RCAS 1")
        header.append("static const struct fsr4_pass_info fsr4_rcas_pass = {fsr4_code%d,sizeof(fsr4_code%d),"
                      "FSR4_CONSTANTS_RCAS,%d,fsr4_bindings%d,FSR4_GROUPS_RCAS,0,0,0};"
                      % (PASSES, PASSES, len(bindings), PASSES))
        identities.append(dict(index=PASSES, entry=dispatch["entryPoint"], dxil_sha256=sha(dxil.read_bytes()),
                               spirv_sha256=sha(code), constants="RCAS", bindings=bindings,
                               groups=("RCAS", 0, 0, 0), reference_groups=dispatch["dispatchDimension"]))
    padded = weights + b"\0" * (-len(weights) % 4)
    header.append("static const uint32_t fsr4_weights[] = {" + ",".join(
        hex(w) for w in struct.unpack(f"<{len(padded) // 4}I", padded)) + "};")
    header.append(f"#define FSR4_WEIGHTS_BYTES {len(weights)}u")
    table = "\n".join(header) + "\n"
    (out / "fsr4_passes.h").write_text(table)
    manifest = dict(capture_graph_sha256=json.loads((capture / "complete.json").read_text())["graph_sha256"],
                    original_graph_sha256=json.loads((original / "complete.json").read_text())["graph_sha256"],
                    fp32_fma=args.fp32_fma, model_guards=bool(args.keep_model_guards),
                    int8_kernels=sorted(generated),
                    weights_sha256=sha(weights), table_sha256=sha(table.encode()),
                    converter_patch_sha256=sha((ROOT / "tools/dxil-spirv-fsr4-fp16.patch").read_bytes()),
                    passes=identities)
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(dict(header=str(out / "fsr4_passes.h"), table_sha256=manifest["table_sha256"],
                          weights_sha256=manifest["weights_sha256"]), indent=2))


if __name__ == "__main__":
    main()
