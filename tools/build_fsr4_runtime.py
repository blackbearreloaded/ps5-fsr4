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
import shutil
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
# Dispatches compiled as wave64 by default (the tables header carries the set to src/ps5_fsr4.c):
# network passes 6, 10 and 11. The looped passes 7-9 run faster as wave32 (twice the waves
# hide their weight loads better: 0.119/0.122/0.182 -> 0.115/0.116/0.170 ms); looped pass 6
# as wave64 (0.090 -> 0.066 ms).
WAVE64_DEFAULT = (13, 21, 23)
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


OP_EXT_INST_IMPORT, OP_EXT_INST, OP_TYPE_INT, OP_UCONVERT, OP_BITCAST, OP_IADD = 11, 12, 21, 113, 124, 128
DECORATION_BINDING, DECORATION_DESCRIPTOR_SET, GLSL_SCLAMP, OP_LABEL = 33, 34, 45, 248
# IAdd ISub IMul ShiftRightLogical ShiftRightArithmetic ShiftLeftLogical BitwiseOr BitwiseAnd
PURE_INTEGER_OPS = {128, 130, 132, 194, 195, 196, 197, 199}
# Dwords from the postpass's top-left R0 tap (padded row y, column x) to its pixel's latent,
# which the generated head writes at padded (x + 1, y + 1) of the RA base (962-wide H rows).
LATENT_OFFSET = (fsr4_int8_kernels.BASES["RA"] + (fsr4_int8_kernels.LEVELS["H"][0] + 1) * 16) // 4


def postpass_head(words):
    """Landmarks of the converted postpass's learned head: instruction order, definitions and
    their blocks, the scratch variable, its first access chain (the pixel's top-left tap) and
    the four packed words the head ends in (channels 4n..4n+3, clamped with SClamp and
    bitcast to uint), in channel order."""
    sets, bindings, defs, blocks, first_function, glsl, block = {}, {}, {}, {}, None, None, None
    order = []
    for i, op, count in instructions(words):
        if op == OP_DECORATE and words[i + 2] == DECORATION_DESCRIPTOR_SET:
            sets[words[i + 1]] = words[i + 3]
        elif op == OP_DECORATE and words[i + 2] == DECORATION_BINDING:
            bindings[words[i + 1]] = words[i + 3]
        elif op == OP_EXT_INST_IMPORT and bytes(struct.pack(f"<{count - 2}I", *words[i + 2:i + count])).startswith(b"GLSL.std.450"):
            glsl = words[i + 1]
        elif op == OP_FUNCTION and first_function is None:
            first_function = i
        elif op == OP_LABEL:
            block = words[i + 1]
        if first_function is not None and count >= 3:
            order.append((i, op, count))
            if words[i + 2] not in defs:
                defs[words[i + 2]] = i
                blocks[words[i + 2]] = block
    scratch = next(v for v in sets if sets[v] == 1 and bindings.get(v) == 11)
    chain = next(i for i, op, count in order if op == OP_ACCESS_CHAIN and count == 6 and words[i + 3] == scratch)
    pointer_type, chain_id, zero, tap = words[chain + 1], words[chain + 2], words[chain + 4], words[chain + 5]
    load = next(i for i, op, count in order if op == OP_LOAD and words[i + 3] == chain_id)
    uint = words[load + 1]
    clamps = [words[i + 2] for i, op, count in order
              if op == OP_EXT_INST and words[i + 3] == glsl and words[i + 4] == GLSL_SCLAMP]
    if len(clamps) != 16:
        raise SystemExit(f"postpass: expected the head's 16 SClamps, found {len(clamps)}")
    packed = []
    for clamp in clamps[-4:]:
        convert = [words[i + 2] for i, op, count in order if op == OP_UCONVERT and words[i + 3] == clamp]
        bitcast = [i for i, op, count in order if op == OP_BITCAST and len(convert) == 1 and words[i + 3] == convert[0]]
        if len(bitcast) != 1 or words[bitcast[0] + 1] != uint:
            raise SystemExit("postpass: the head's packed latent words are not where expected")
        packed.append(bitcast[0])
    if len({blocks[words[i + 2]] for i in packed}) != 1:
        raise SystemExit("postpass: the packed latent words are made in different blocks")
    return dict(defs=defs, blocks=blocks, first_function=first_function, scratch=scratch, chain=chain,
                pointer_type=pointer_type, zero=zero, tap=tap, uint=uint, packed=packed)


def route_postpass_latent(code, latent_offset):
    """Make the converted postpass read its learned head's result instead of computing it.

    The first scratch access reads the top-left tap of the pixel's 3x3 neighbourhood; the
    generated head kernel wrote the same pixel's latent `latent_offset` dwords further on.
    Each of the head's packed words becomes a copy of that load, which leaves the whole head
    dead.
    """
    words = list(struct.unpack(f"<{len(code) // 4}I", code))
    head = postpass_head(words)
    defs, blocks, first_function, scratch, uint, packed = (head[k] for k in (
        "defs", "blocks", "first_function", "scratch", "uint", "packed"))
    pointer_type, zero, tap = head["pointer_type"], head["zero"], head["tap"]
    constants = {}
    for i, op, count in instructions(words):
        if op == OP_CONSTANT and count == 4 and words[i + 1] == uint:
            constants.setdefault(words[i + 3], words[i + 2])
    new_constants = []
    for value in (latent_offset, 1, 2, 3):
        if value not in constants:
            constants[value], words[3] = words[3], words[3] + 1
            new_constants += [(4 << 16) | OP_CONSTANT, uint, constants[value], value]
    # The tap's address is computed inside that tap's bounds check; its inputs (the pixel's
    # coordinates) come from blocks that dominate the head. Recompute it where the packed
    # words are made.
    clones, renamed = [], {}

    def clone(ident):
        if ident in renamed or ident not in defs or blocks[ident] != blocks[tap]:
            return
        i = defs[ident]
        op, count = words[i] & 0xffff, words[i] >> 16
        if op not in PURE_INTEGER_OPS:
            raise SystemExit(f"postpass: unexpected opcode {op} in the tap address")
        for operand in words[i + 3:i + count]:
            clone(operand)
        renamed[ident], words[3] = words[3], words[3] + 1
        clones.extend([words[i], words[i + 1], renamed[ident]] + [renamed.get(o, o) for o in words[i + 3:i + count]])

    clone(tap)
    base, words[3] = words[3], words[3] + 1
    loads = clones + [(5 << 16) | OP_IADD, uint, base, renamed[tap], constants[latent_offset]]
    latent = []
    for n in range(4):
        index = base
        if n:
            index, words[3] = words[3], words[3] + 1
            loads += [(5 << 16) | OP_IADD, uint, index, base, constants[n]]
        pointer, value, words[3] = words[3], words[3] + 1, words[3] + 2
        loads += [(6 << 16) | OP_ACCESS_CHAIN, pointer_type, pointer, scratch, zero, index,
                  (4 << 16) | OP_LOAD, uint, value, pointer]
        latent.append(value)
    result = words[:5]
    for i, op, count in instructions(words):
        if i == first_function:
            result += new_constants
        if i == min(packed):
            result += loads
        if i in packed:
            result += [(4 << 16) | OP_COPY_OBJECT, uint, words[i + 2], latent[packed.index(i)]]
        else:
            result += words[i:i + count]
    return struct.pack(f"<{len(result)}I", *result)


OP_ENTRY_POINT, OP_EXECUTION_MODE, OP_TYPE_VECTOR, OP_TYPE_FUNCTION = 15, 16, 23, 33
OP_FUNCTION_PARAMETER, OP_FUNCTION_END, OP_FUNCTION_CALL, OP_IMUL = 55, 56, 57, 132
OP_TYPE_POINTER, OP_VARIABLE, OP_STORE, STORAGE_FUNCTION, BUILTIN_WORKGROUP_SIZE = 32, 59, 62, 7, 25
OP_UGREATER_THAN_EQUAL, OP_SHIFT_RIGHT_LOGICAL, OP_SHIFT_LEFT_LOGICAL, OP_EXECUTION_MODE_ID = 174, 194, 196, 331
CAPABILITY_LINKAGE, DECORATION_LINKAGE_ATTRIBUTES, LINKAGE_EXPORT, LINKAGE_IMPORT = 5, 41, 0, 1


def spirv_string(text):
    data = text.encode() + b"\0"
    data += b"\0" * (-len(data) % 4)
    return list(struct.unpack(f"<{len(data) // 4}I", data))


def read_string(words):
    data = struct.pack(f"<{len(words)}I", *words)
    return data[:data.index(b"\0")].decode()


def add_linkage(words, target, name, linkage_type):
    """Words with the Linkage capability and target's LinkageAttributes declared, after the
    module's last capability and last decoration."""
    capabilities = [i for i, op, _ in instructions(words) if op == OP_CAPABILITY]
    first_function = next(i for i, op, _ in instructions(words) if op == OP_FUNCTION)
    annotations = [i for i, op, _ in instructions(words) if op in (OP_DECORATE, OP_MEMBER_DECORATE) and i < first_function]
    decoration = [target, DECORATION_LINKAGE_ATTRIBUTES] + spirv_string(name) + [linkage_type]
    result = words[:5]
    for i, op, count in instructions(words):
        result += words[i:i + count]
        if i == capabilities[-1]:
            result += [(2 << 16) | OP_CAPABILITY, CAPABILITY_LINKAGE]
        if i == annotations[-1]:
            result += [((len(decoration) + 1) << 16) | OP_DECORATE] + decoration
    return result


def call_postpass_head(code, name):
    """Make the converted postpass take its learned head's four packed words from an imported
    function `name`(x, y, ext_x, ext_y) returning a uvec4, which leaves the FP32 head dead.

    The first scratch access reads the pixel's top-left tap at dword (962 * 16 * y + 16 * x) / 4
    of H pixel (x, y), the tap's -1 offset and the tensor's +1 border cancelling; the head runs
    only where x < ext_x and y < ext_y, the current H extent.
    """
    words = list(struct.unpack(f"<{len(code) // 4}I", code))
    head = postpass_head(words)
    defs, uint = head["defs"], head["uint"]
    constants = {words[i + 2] for i, op, _ in instructions(words) if op == OP_CONSTANT}

    def producer(ident, opcode):
        i = defs.get(ident)
        if i is None or words[i] & 0xffff != opcode:
            raise SystemExit(f"postpass: unexpected code before the head's top-left tap (%{ident})")
        return i

    add = producer(words[producer(words[head["chain"] + 5], OP_SHIFT_RIGHT_LOGICAL) + 3], OP_IADD)
    x = y = None
    for operand in words[add + 3:add + 5]:
        i = defs.get(operand)
        if i is not None and words[i] & 0xffff == OP_IMUL:
            y = next((o for o in words[i + 3:i + 5] if o not in constants), None)
        elif i is not None and words[i] & 0xffff == OP_SHIFT_LEFT_LOGICAL:
            x = words[i + 3]
    extent = {}
    for i, op, _ in instructions(words):
        if op == OP_UGREATER_THAN_EQUAL and words[i + 3] in (x, y):
            extent.setdefault(words[i + 3], words[i + 4])
    if x is None or y is None or set(extent) != {x, y}:
        raise SystemExit("postpass: the head's pixel coordinates or extent check were not found")
    uvec4 = next(words[i + 1] for i, op, _ in instructions(words)
                 if op == OP_TYPE_VECTOR and words[i + 2] == uint and words[i + 3] == 4)
    # glslang passes GLSL in-parameters as pointers to function-local copies: so does the call.
    pointer = next((words[i + 1] for i, op, _ in instructions(words)
                    if op == OP_TYPE_POINTER and words[i + 2] == STORAGE_FUNCTION and words[i + 3] == uint), None)
    new_pointer = pointer is None
    if new_pointer:
        pointer, words[3] = words[3], words[3] + 1
    function_type, function, call = words[3], words[3] + 1, words[3] + 2
    params, local = list(range(words[3] + 3, words[3] + 7)), list(range(words[3] + 7, words[3] + 11))
    words[3] += 11
    entry_label = next(i for i, op, _ in instructions(words) if op == OP_LABEL and i > head["first_function"])
    arguments = (x, y, extent[x], extent[y])
    result = words[:5]
    for i, op, count in instructions(words):
        if i == head["first_function"]:
            if new_pointer:
                result += [(4 << 16) | OP_TYPE_POINTER, pointer, STORAGE_FUNCTION, uint]
            result += [(7 << 16) | OP_TYPE_FUNCTION, function_type, uvec4] + [pointer] * 4
            result += [(5 << 16) | OP_FUNCTION, uvec4, function, 0, function_type]
            for p in params:
                result += [(3 << 16) | OP_FUNCTION_PARAMETER, pointer, p]
            result += [(1 << 16) | OP_FUNCTION_END]
        if i == min(head["packed"]):
            for variable, value in zip(local, arguments):
                result += [(3 << 16) | OP_STORE, variable, value]
            result += [(8 << 16) | OP_FUNCTION_CALL, uvec4, call, function] + local
        if i in head["packed"]:
            result += [(5 << 16) | OP_COMPOSITE_EXTRACT, uint, words[i + 2], call, head["packed"].index(i)]
        else:
            result += words[i:i + count]
        if i == entry_label:
            for variable in local:
                result += [(4 << 16) | OP_VARIABLE, pointer, variable, STORAGE_FUNCTION]
    result = add_linkage(result, function, name, LINKAGE_IMPORT)
    return struct.pack(f"<{len(result)}I", *result)


def export_function(code, name):
    """The compiled head as a library exporting `name` (glslang names it `name(...`): its stub
    entry point, main and their execution modes dropped, Linkage declared."""
    words = list(struct.unpack(f"<{len(code) // 4}I", code))
    names = {words[i + 1]: read_string(words[i + 2:i + count]) for i, op, count in instructions(words) if op == OP_NAME}
    main = next(words[i + 2] for i, op, _ in instructions(words) if op == OP_ENTRY_POINT)
    function = next(ident for ident, text in names.items() if text.startswith(name + "("))
    stub, in_main = {main}, False  # ids the stub main defines, whose names and decorations go with it
    for i, op, count in instructions(words):
        in_main = in_main or (op == OP_FUNCTION and words[i + 2] == main)
        if in_main and count >= 3 and op not in (OP_STORE, OP_FUNCTION_END):
            stub.add(words[i + 2] if op != OP_LABEL else words[i + 1])
        in_main = in_main and op != OP_FUNCTION_END
    result, in_main = words[:5], False
    for i, op, count in instructions(words):
        # A WorkgroupSize built-in would override the linking module's LocalSize.
        if op in (OP_ENTRY_POINT, OP_EXECUTION_MODE, OP_EXECUTION_MODE_ID) or \
                (op in (OP_NAME, OP_DECORATE, OP_MEMBER_NAME) and words[i + 1] in stub) or \
                (op == OP_DECORATE and words[i + 2] == DECORATION_BUILTIN and words[i + 3] == BUILTIN_WORKGROUP_SIZE):
            continue
        if op == OP_FUNCTION and words[i + 2] == main:
            in_main = True
        if in_main:
            in_main = op != OP_FUNCTION_END
            continue
        result += words[i:i + count]
    result = add_linkage(result, function, name, LINKAGE_EXPORT)
    return struct.pack(f"<{len(result)}I", *result)


def drop_linkage(code):
    words = list(struct.unpack(f"<{len(code) // 4}I", code))
    result = words[:5]
    for i, op, count in instructions(words):
        if not ((op == OP_CAPABILITY and words[i + 1] == CAPABILITY_LINKAGE) or
                (op == OP_DECORATE and words[i + 2] == DECORATION_LINKAGE_ATTRIBUTES)):
            result += words[i:i + count]
    return struct.pack(f"<{len(result)}I", *result)


def fuse_postpass_head(code, model, scratch, out):
    """The converted postpass with its FP32 learned head replaced by the generated INT8 head,
    linked in as a function; returns validated SPIR-V whose FP32 head is gone."""
    spv, head_glsl, head_spv, linked = (out / "pass27.spv", out / "posthead-function.comp",
                                        out / "posthead-function.spv", out / "pass27-linked.spv")
    spv.write_bytes(call_postpass_head(code, fsr4_int8_kernels.POSTHEAD_FUNCTION))
    source, groups = fsr4_int8_kernels.posthead_function(model, scratch)
    head_glsl.write_text(source)
    # SPIR-V 1.3 like the converted shaders, so the linked module keeps their version.
    subprocess.run([str(TOOLCHAIN_BIN / "glslangValidator"), "--target-env", "vulkan1.1", "-o", str(head_spv),
                    str(head_glsl)], check=True, stdout=subprocess.DEVNULL)
    head_spv.write_bytes(export_function(head_spv.read_bytes(), fsr4_int8_kernels.POSTHEAD_FUNCTION))
    link = shutil.which("spirv-link")
    if not link:
        raise SystemExit("--int8-postpass-head needs spirv-link (SPIRV-Tools) on PATH")
    subprocess.run([link, "--target-env", "vulkan1.1", str(spv), str(head_spv), "-o", str(linked)], check=True)
    spv.write_bytes(drop_linkage(linked.read_bytes()))
    code = strip_dead_code(spv, False)  # inlines the head
    spv.write_bytes(remove_dead_code(code))
    code = strip_dead_code(spv, False)
    if any(op == OP_EXT_INST and w[i + 4] == GLSL_SCLAMP
           for w in [struct.unpack(f"<{len(code) // 4}I", code)] for i, op, _ in instructions(w)):
        raise SystemExit("postpass: the FP32 learned head survived dead-code removal")
    print(f"pass 27: learned head replaced by the generated INT8 head, {groups} i16 groups", file=sys.stderr)
    return code


# Side-effect-free instructions with a result: loads and access chains, conversions,
# composites, arithmetic, bit and comparison operations, selects, phis and GLSL.std.450
# ext-insts (the only extended set dxil-spirv emits here).
PURE_OPS = {12, 61, 65, 77, 79, 80, 81, 82, 83, 109, 110, 111, 112, 113, 114, 115, 124, 126, 127, 128, 129,
            130, 131, 132, 133, 134, 135, 136, 137, 138, 139, 140, 141, 164, 165, 166, 167, 168, 169, 170, 171,
            172, 173, 174, 175, 176, 177, 178, 179, 180, 181, 182, 183, 184, 185, 186, 187, 188, 189, 190, 191,
            194, 195, 196, 197, 198, 199, 200, 201, 202, 203, 204, 245}
OP_NAME, OP_MEMBER_NAME, OP_MEMBER_DECORATE = 5, 6, 72


def remove_dead_code(code):
    """Drop side-effect-free instructions whose results are unused, until none is left.

    spirv-opt's aggressive pass keeps the converted postpass's dead learned head, so this
    simpler pass removes it; empty branches are left for the backend compiler.
    """
    words = list(struct.unpack(f"<{len(code) // 4}I", code))
    first_function = next(i for i, op, _ in instructions(words) if op == OP_FUNCTION)
    uses, producer, operands = {}, {}, {}
    for i, op, count in instructions(words):
        if i < first_function:
            continue
        if op in PURE_OPS:
            producer[words[i + 2]] = i
            operands[i] = words[i + 3:i + count]
            refs = operands[i]
        else:
            refs = words[i + 1:i + count]
        for ref in refs:
            uses[ref] = uses.get(ref, 0) + 1
    dead = set()
    work = [i for result, i in producer.items() if not uses.get(result)]
    while work:
        i = work.pop()
        if i in dead:
            continue
        dead.add(i)
        for ref in operands[i]:
            uses[ref] -= 1
            if not uses[ref] and ref in producer and producer[ref] not in dead:
                work.append(producer[ref])
    removed = {words[i + 2] for i in dead}
    result = words[:5]
    for i, op, count in instructions(words):
        if i in dead or (op in (OP_NAME, OP_DECORATE, OP_MEMBER_NAME, OP_MEMBER_DECORATE) and words[i + 1] in removed):
            continue
        result += words[i:i + count]
    return struct.pack(f"<{len(result)}I", *result)


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


def generate_kernel(index, model, bindings, out, tables, use_table=False, banks=None, looped=False):
    """Packed-i16 kernel for dispatch `index`, compiled to validated SPIR-V; returns its words.

    With `use_table`, a kernel that binds the model reads its weight pairs from a table
    appended after it to `tables` (the words already placed there). `banks` splits its
    outputs over that many workgroup layers; `looped` generates the loop form, which
    always streams its weights from a table."""
    table = None
    if (use_table or looped) and any(role == "WEIGHTS" for _, _, _, role in bindings):
        table = (len(model) + 3) // 4 + len(tables)
    source, groups, pairs = fsr4_int8_kernels.generate(index, model, bindings, table, banks, looped)
    tables += pairs
    glsl, spv = out / f"pass{index}.comp", out / f"pass{index}.spv"
    glsl.write_text(source)
    subprocess.run([str(TOOLCHAIN_BIN / "glslangValidator"), "--target-env", "vulkan1.3", "-o", str(spv), str(glsl)],
                   check=True, stdout=subprocess.DEVNULL)
    subprocess.run([str(TOOLCHAIN_BIN / "spirv-val"), "--target-env", "vulkan1.3", str(spv)], check=True)
    print(f"pass {index}: generated packed-i16 kernel{' (looped)' if looped else ''}, {groups} i16 groups",
          file=sys.stderr)
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
    parser.add_argument("--int8-kernels", default=",".join(map(str, fsr4_int8_kernels.DEFAULT)),
                        help="Comma-separated dispatch indices whose converted shader is replaced by a "
                             f"generated packed-i16 kernel (available: {sorted(fsr4_int8_kernels.PASSES)}; "
                             "an empty value keeps every converted shader)")
    parser.add_argument("--weight-tables", nargs="?", const="all",
                        default=",".join(map(str, fsr4_int8_kernels.DEFAULT_TABLES)),
                        help="Generated kernels (all, or the comma-separated dispatch indices given) read "
                             "their weight pairs from tables after the model instead of instruction literals")
    parser.add_argument("--int8-banks", default="",
                        help="dispatch:banks pairs, e.g. 13:4: that generated kernel splits its outputs "
                             "over as many workgroup layers")
    parser.add_argument("--int8-postpass-head", action=argparse.BooleanOptionalAction, default=True,
                        help="Link the generated INT8 learned head into the converted postpass in place "
                             "of its FP32 head (needs spirv-link; off when dispatch 26 is generated)")
    parser.add_argument("--wave64", default=",".join(map(str, WAVE64_DEFAULT)),
                        help="Comma-separated dispatch indices compiled as wave64 where the driver offers it "
                             "(the rest run as wave32)")
    parser.add_argument("--int8-loops", default=",".join(map(str, fsr4_int8_kernels.DEFAULT_LOOPS)),
                        help="Comma-separated dispatch indices generated in loop form, weights streamed "
                             f"from a table (available: {list(fsr4_int8_kernels.LOOPED)}; an empty value "
                             "generates none)")
    args = parser.parse_args()
    looped = {int(i) for i in args.int8_loops.split(",") if i}
    wave64 = {int(i) for i in args.wave64.split(",") if i}
    if wave64 - set(range(PASSES)):
        raise SystemExit(f"--wave64 names dispatches outside 0-{PASSES - 1}: {sorted(wave64 - set(range(PASSES)))}")
    generated = {int(i) for i in args.int8_kernels.split(",") if i} | looped
    tables = []
    banked = {int(k): int(v) for k, v in (item.split(":") for item in args.int8_banks.split(",") if item)}
    if set(banked) - generated:
        raise SystemExit(f"banks given for dispatches that are not generated: {sorted(set(banked) - generated)}")
    if looped - set(fsr4_int8_kernels.LOOPED) or looped & set(banked):
        raise SystemExit(f"no loop form for dispatches {sorted(looped - set(fsr4_int8_kernels.LOOPED))}, "
                         f"or banks given for looped ones {sorted(looped & set(banked))}")
    if generated - set(fsr4_int8_kernels.PASSES):
        raise SystemExit(f"no generated kernel for dispatch {sorted(generated - set(fsr4_int8_kernels.PASSES))}")
    # A generated dispatch 26 computes the head separately; the postpass then reads its latent.
    args.int8_postpass_head = args.int8_postpass_head and fsr4_int8_kernels.POSTHEAD not in generated
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
            use_table = args.weight_tables == "all" or str(index) in args.weight_tables.split(",")
            code = generate_kernel(index, weights, bindings, out, tables, use_table, banked.get(index),
                                   index in looped)
        if index == fsr4_int8_kernels.POSTPASS and fsr4_int8_kernels.POSTHEAD in generated:
            spv = out / f"pass{index}.spv"
            spv.write_bytes(remove_dead_code(route_postpass_latent(code, LATENT_OFFSET)))
            code = strip_dead_code(spv, False)
            if any(op == OP_EXT_INST and w[i + 4] == GLSL_SCLAMP
                   for w in [struct.unpack(f"<{len(code) // 4}I", code)] for i, op, _ in instructions(w)):
                raise SystemExit("postpass: the learned head survived dead-code removal")
        elif index == fsr4_int8_kernels.POSTPASS and args.int8_postpass_head:
            scratch = next((s, b) for s, b, _, role in bindings if role == "SCRATCH")
            code = fuse_postpass_head(code, weights, scratch, out)
        emit_code(header, index, code, bindings)
        rule = group_rule(index, dispatch, dxil, output)
        if index == fsr4_int8_kernels.POSTHEAD and index in generated:
            rule = ("NETWORK", 1, 0, 0)  # the head runs per H pixel, not over the border
        elif index == fsr4_int8_kernels.POSTHEAD and args.int8_postpass_head:
            # Only the postpass head read the border this pass clears; the linked head checks its
            # own extent, so the pass is not dispatched.
            rule = ("NONE", 0, 0, 0)
        if index in banked:
            rule = (rule[0], rule[1], banked[index], rule[3])  # output banks as workgroup layers
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
    # The model, then the generated kernels' weight-pair tables: all uploaded to the model buffer.
    padded = weights + b"\0" * (-len(weights) % 4) + struct.pack(f"<{len(tables)}I", *tables)
    header.append("static const uint32_t fsr4_weights[] = {" + ",".join(
        hex(w) for w in struct.unpack(f"<{len(padded) // 4}I", padded)) + "};")
    header.append(f"#define FSR4_WEIGHTS_BYTES {len(padded) if tables else len(weights)}u")
    header.append(f"#define FSR4_WAVE64_PASSES 0x{sum(1 << i for i in wave64):x}u")
    table = "\n".join(header) + "\n"
    (out / "fsr4_passes.h").write_text(table)
    manifest = dict(capture_graph_sha256=json.loads((capture / "complete.json").read_text())["graph_sha256"],
                    original_graph_sha256=json.loads((original / "complete.json").read_text())["graph_sha256"],
                    fp32_fma=args.fp32_fma, model_guards=bool(args.keep_model_guards),
                    int8_kernels=sorted(generated), int8_loops=sorted(looped), weight_table_words=len(tables),
                    wave64=sorted(wave64), int8_postpass_head=args.int8_postpass_head,
                    int8_banks={str(k): v for k, v in sorted(banked.items())},
                    weights_sha256=sha(weights), table_sha256=sha(table.encode()),
                    converter_patch_sha256=sha((ROOT / "tools/dxil-spirv-fsr4-fp16.patch").read_bytes()),
                    passes=identities)
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(dict(header=str(out / "fsr4_passes.h"), table_sha256=manifest["table_sha256"],
                          weights_sha256=manifest["weights_sha256"]), indent=2))


if __name__ == "__main__":
    main()
