# Copyright (C) 2026 BlackBearReloaded
# SPDX-License-Identifier: GPL-3.0-or-later
"""Generate packed-i16 GLSL for the INT8 network passes from the local model.

gfx1013 has no dot2/dot4 instructions, so a signed INT8 product is best issued as
v_pk_mad_u16: one activation broadcast to both 16-bit lanes, multiplied by a
literal pair holding the weights of two output channels. A pair accumulates in
its i16 lanes while an exact interval bound proves neither lane can leave
[-32768, 32767] (ordered first-fit decreasing, channels paired by weight mass),
then each lane widens into an i32 sum that starts at the bias. Requantization is
the network's signed round-half-to-even shift followed by INT8 saturation.

The kernels are drop-in replacements for the converted passes: same bindings,
dispatch grid, bounds and scratch addresses. They bake the model's weights, so
the generated sources are written to the build tree only.
"""
import struct

I16_MIN, I16_MAX = -32768, 32767
# Band-0 scratch layout: padded tensor sizes (width, height) and base anchors, in bytes.
LEVELS = {"H": (962, 542), "Q": (482, 272), "E": (242, 137)}
BASES = {"R0": 0, "RA": 8_342_464, "RB": 12_537_792, "RC": 14_659_648}
# Logical band-0 extents: invocations beyond them return, as in the converted passes.
EXTENTS = {"H": (960, 540), "Q": (480, 270), "E": (240, 135)}


def signed_bytes(model, start, count):
    return list(struct.unpack_from(f"<{count}b", model, start))


def int32s(model, start, count):
    return list(struct.unpack_from(f"<{count}i", model, start))


def step(weight, low, high):
    """Range a lane moves by when adding weight * activation, activation in [low, high]."""
    return min(weight * low, weight * high), max(weight * low, weight * high)


def weight_mass(row, low, high):
    return sum(max(-a, b) for a, b in (step(w, low, high) for w in row))


def pair_channels(rows, low, high):
    """Pair output channels of similar weight mass so both lanes fill their groups alike."""
    order = sorted(range(len(rows)), key=lambda oc: weight_mass(rows[oc], low, high))
    return [(order[k], order[k + 1]) for k in range(0, len(order) - 1, 2)]


def group_pair(row0, row1, ranges):
    """Inputs of one channel pair split into groups whose i16 lanes cannot overflow.

    ranges[i] is the (low, high) interval of input i. First-fit decreasing on the
    larger lane step; zero weight pairs are dropped.
    """
    items = []
    for i, (w0, w1) in enumerate(zip(row0, row1)):
        if w0 or w1:
            s0, s1 = step(w0, *ranges[i]), step(w1, *ranges[i])
            items.append((max(-s0[0], s0[1], -s1[0], s1[1]), i, s0, s1))
    items.sort(key=lambda item: (-item[0], item[1]))
    groups = []
    for _, i, s0, s1 in items:
        for g in groups:
            lo0, hi0, lo1, hi1 = g["range"]
            if (lo0 + s0[0] >= I16_MIN and hi0 + s0[1] <= I16_MAX and
                    lo1 + s1[0] >= I16_MIN and hi1 + s1[1] <= I16_MAX):
                g["range"] = (lo0 + s0[0], hi0 + s0[1], lo1 + s1[0], hi1 + s1[1])
                g["inputs"].append(i)
                break
        else:
            groups.append({"range": (s0[0], s0[1], s1[0], s1[1]), "inputs": [i]})
    for g in groups:
        g["inputs"].sort()
    return [g["inputs"] for g in groups]


class Kernel:
    """GLSL source under construction for one pass."""

    def __init__(self, name):
        self.name = name
        self.lines = []
        self.counter = 0
        self.groups = 0

    def emit(self, line):
        self.lines.append("  " + line)

    def temp(self, prefix):
        self.counter += 1
        return f"{prefix}{self.counter}"

    def source(self, bindings):
        head = ["#version 460",
                "#extension GL_EXT_shader_explicit_arithmetic_types_int16 : require",
                f"// {self.name}: generated from the local model, do not distribute",
                "layout(local_size_x = 64) in;"]
        for set_index, binding, kind in bindings:
            if kind == "model":
                head.append(f"layout(set = {set_index}, binding = {binding}) readonly buffer Model {{ uint model_words[]; }};")
            elif kind == "scratch":
                head.append(f"layout(set = {set_index}, binding = {binding}) buffer Scratch {{ uvec4 scratch[]; }};")
            elif kind == "tensor":
                head.append(f"layout(set = {set_index}, binding = {binding}) uniform Tensor {{ uvec4 tensor_rows[17]; }};")
        head += ["int rne(int x, int s) { return (x + ((1 << (s - 1)) - 1) + ((x >> s) & 1)) >> s; }",
                 "uint pack4(int a, int b, int c, int d) {",
                 "  return (uint(a) & 0xffu) | ((uint(b) & 0xffu) << 8) | ((uint(c) & 0xffu) << 16) | (uint(d) << 24);",
                 "}", "void main() {"]
        return "\n".join(head + self.lines + ["}"]) + "\n"

    # Scratch access, in 16-byte units.
    def tensor_index(self, base, level, x, y):
        width, _ = LEVELS[level]
        return f"{BASES[base] // 16}u + ({y} + 1u) * {width}u + {x} + 1u"

    def plane(self, level):
        width, height = LEVELS[level]
        return width * height

    def load_bytes(self, base, level, channels, x, y):
        """Signed bytes of one pixel's first channels as i16 expressions (broadcastable lanes)."""
        return self.load_bytes_banks(base, level, range(channels // 16), x, y)

    def load_bytes_banks(self, base, level, banks, x, y):
        """Signed bytes of one pixel's 16-channel banks as i16 expressions."""
        index = self.temp("at")
        self.emit(f"uint {index} = {self.tensor_index(base, level, x, y)};")
        values = []
        for bank in banks:
            vec = self.temp("v")
            self.emit(f"uvec4 {vec} = scratch[{index} + {bank * self.plane(level)}u];")
            for k in range(4):
                raw = self.temp("r")
                self.emit(f"i16vec2 {raw} = i16vec2(unpack16({vec}[{k}]));")
                self.emit(f"i16vec2 {raw}o = {raw} >> int16_t(8);")
                self.emit(f"i16vec2 {raw}e = ({raw} << int16_t(8)) >> int16_t(8);")
                values += [f"{raw}e.x", f"{raw}o.x", f"{raw}e.y", f"{raw}o.y"]
        return values

    def store_bytes(self, base, level, values, x, y):
        index = self.temp("to")
        self.emit(f"uint {index} = {self.tensor_index(base, level, x, y)};")
        for bank in range(len(values) // 16):
            words = [f"pack4({', '.join(values[bank * 16 + k * 4:bank * 16 + k * 4 + 4])})" for k in range(4)]
            self.emit(f"scratch[{index} + {bank * self.plane(level)}u] = uvec4({', '.join(words)});")

    def layer(self, inputs, ranges, rows, bias, shifts, relu=False, packed=False, block=None, done=None):
        """Requantized outputs of bias[oc] + sum_i rows[oc][i] * inputs[i].

        Each channel pair is accumulated, requantized (round-half-even shift, optional
        ReLU, INT8 saturation) and, with `packed`, packed into one i16 pair before the
        next pair starts, so only finished 8-bit results stay live. With `block`,
        channels pair only within consecutive blocks of that many channels and
        done(first_channel, outputs) runs after each block. Returns one expression
        per output channel: an int, or with `packed` an i16 lane.
        """
        low = min(r[0] for r in ranges)
        high = max(r[1] for r in ranges)
        out = [None] * len(rows)
        block = block or len(rows)
        for first in range(0, len(rows), block):
            self._pairs(inputs, ranges, rows, bias, shifts, relu, packed, out, first, block, low, high)
            if done:
                done(first, out)
        return out

    def _pairs(self, inputs, ranges, rows, bias, shifts, relu, packed, out, first, block, low, high):
        local = pair_channels(rows[first:first + block], low, high)
        for oc0, oc1 in ((first + a, first + b) for a, b in local):
            sums = [self.temp("s"), self.temp("s")]
            self.emit(f"int {sums[0]} = {bias[oc0]}, {sums[1]} = {bias[oc1]};")
            acc = self.temp("g")
            self.emit(f"i16vec2 {acc};")
            for group in group_pair(rows[oc0], rows[oc1], ranges):
                self.groups += 1
                for n, i in enumerate(group):
                    op = "=" if n == 0 else "+="
                    self.emit(f"{acc} {op} i16vec2({inputs[i]}) * i16vec2(int16_t({rows[oc0][i]}), int16_t({rows[oc1][i]}));")
                self.emit(f"{sums[0]} += int({acc}.x); {sums[1]} += int({acc}.y);")
            results = []
            for oc, s in zip((oc0, oc1), sums):
                name = self.temp("q")
                value = f"max({s}, 0)" if relu else s
                self.emit(f"int {name} = clamp(rne({value}, {shifts[oc]}), -128, 127);")
                results.append(name)
            if packed:
                name = self.temp("p")
                self.emit(f"i16vec2 {name} = i16vec2(int16_t({results[0]}), int16_t({results[1]}));")
                out[oc0], out[oc1] = f"{name}.x", f"{name}.y"
            else:
                out[oc0], out[oc1] = results

    def store_bank(self, index, level, bank, values):
        """One 16-channel bank of int results at a precomputed tensor index."""
        words = [f"pack4({', '.join(values[k * 4:k * 4 + 4])})" for k in range(4)]
        self.emit(f"scratch[{index} + {bank * self.plane(level)}u] = uvec4({', '.join(words)});")

    def raw_bank(self, base, level, bank, x, y):
        """One bank loaded as a uvec4, without unpacking."""
        vec = self.temp("c")
        self.emit(f"uvec4 {vec} = scratch[{self.tensor_index(base, level, x, y)} + {bank * self.plane(level)}u];")
        return vec


def down2x2(model, spec):
    """Learned 2x2 stride-2 downsampler (converted pass with one spatial layer)."""
    cin, cout = spec["cin"], spec["cout"]
    w = signed_bytes(model, spec["weights"], 4 * cout * cin)
    bias = int32s(model, spec["bias"], cout)
    rows = [[w[(t * cout + oc) * cin + ic] for t in range(4) for ic in range(cin)] for oc in range(cout)]
    k = Kernel(spec["name"])
    width, height = EXTENTS[spec["output"][1]]
    k.emit("uint x = gl_WorkGroupID.x * 64u + gl_LocalInvocationID.x;")
    k.emit("uint y = gl_WorkGroupID.y;")
    k.emit(f"if (x >= {width}u || y >= {height}u) return;")
    inputs = []
    for ky in range(2):
        for kx in range(2):
            inputs += k.load_bytes(spec["input"][0], spec["input"][1], cin, f"(2u * x + {kx}u)", f"(2u * y + {ky}u)")
    out = k.layer(inputs, [(-128, 127)] * len(inputs), rows, bias, [spec["shift"]] * cout)
    k.store_bytes(spec["output"][0], spec["output"][1], out, "x", "y")
    return k


def start(k, level):
    """Invocation coordinates; lanes beyond the band's extent return, as in the converted passes."""
    width, height = EXTENTS[level]
    k.emit("uint x = gl_WorkGroupID.x * 64u + gl_LocalInvocationID.x;")
    k.emit("uint y = gl_WorkGroupID.y;")
    k.emit(f"if (x >= {width}u || y >= {height}u) return;")


def skips(k, base, level, banks, x, y):
    """Residual skip bytes of one pixel, read raw and extracted per channel."""
    raw = [k.raw_bank(base, level, bank, x, y) for bank in banks]
    return [f"bitfieldExtract(int({raw[c // 16]}[{c % 16 // 4}]), {8 * (c % 4)}, 8)" for c in range(16 * len(banks))]


def wide_block(model, spec):
    """64-channel residual block stored to scratch (passes 7 and 8)."""
    k = Kernel(spec["name"])
    level = spec["input"][1]
    start(k, level)
    target = k.temp("to")
    k.emit(f"uint {target} = {k.tensor_index(spec['output'][0], level, 'x', 'y')};")
    wide_prefix(k, model, spec, block=16,
                done=lambda first, out: k.store_bank(target, level, first // 16, out[first:first + 16]))
    return k


def wide_upsample(model, spec):
    """64-channel residual block followed by a learned 2x2 sub-pixel projection with a skip
    (pass 9): each invocation writes the four sub-pixel phases of its position."""
    k = Kernel(spec["name"])
    start(k, spec["input"][1])
    prefix = wide_prefix(k, model, spec, packed=True)
    # Only positions inside the current tensor extent project; each phase stays inside the band.
    k.emit(f"uvec2 extent = tensor_rows[{spec['extent_row']}].xy;")
    k.emit("if (x >= extent.x || y >= extent.y) return;")
    offset, bias_offset, shift, factor = spec["up"]
    cin, cout = 64, spec["up_channels"]
    w = signed_bytes(model, offset, 4 * cout * cin)
    bias = int32s(model, bias_offset, cout)
    out_base, out_level = spec["output"]
    width, height = EXTENTS[out_level]
    for py in range(2):
        for px in range(2):
            qx, qy = f"(2u * x + {px}u)", f"(2u * y + {py}u)"
            k.emit(f"if ({qx} < {width}u && {qy} < {height}u) {{")
            skip = skips(k, spec["skip"][0], out_level, range(cout // 16), qx, qy)
            phase = py * 2 + px
            rows = [w[(phase * cout + oc) * cin:(phase * cout + oc + 1) * cin] for oc in range(cout)]
            target = k.temp("to")
            k.emit(f"uint {target} = {k.tensor_index(out_base, out_level, qx, qy)};")
            k.layer(prefix, [(-128, 127)] * cin, rows, [f"{bias[oc]} + {skip[oc]} * {factor}" for oc in range(cout)],
                    [shift] * cout, block=16,
                    done=lambda first, out, t=target: k.store_bank(t, out_level, first // 16, out[first:first + 16]))
            k.emit("}")
    return k


def wide_prefix(k, model, spec, packed=False, block=None, done=None):
    """Grouped 3x3 on channels 0-31 (two groups of 16), concat of channels 32-63, 1x1 64->128
    with ReLU, 1x1 128->64 and the per-channel residual requantization; returns the 64 outputs."""
    level = spec["input"][1]
    base = spec["input"][0]
    w0 = signed_bytes(model, spec["spatial"][0], 9 * 32 * 16)
    b0 = int32s(model, spec["spatial"][1], 32)
    learned = []
    for g in range(2):  # each spatial group reads only its own bank of the 3x3 neighbourhood
        inputs = []
        for ky in range(3):
            for kx in range(3):
                inputs += k.load_bytes_banks(base, level, (g,), f"(x + {kx}u - 1u)", f"(y + {ky}u - 1u)")
        rows = [[w0[((ky * 3 + kx) * 32 + oc) * 16 + ic] for ky in range(3) for kx in range(3) for ic in range(16)]
                for oc in range(g * 16, g * 16 + 16)]
        learned += k.layer(inputs, [(-128, 127)] * len(inputs), rows, b0[g * 16:g * 16 + 16],
                           [spec["spatial"][2]] * 16, packed=True)
    bypass = k.load_bytes_banks(base, level, (2, 3), "x", "y")
    w1 = signed_bytes(model, spec["expand"][0], 128 * 64)
    b1 = int32s(model, spec["expand"][1], 128)
    hidden = k.layer(learned + bypass, [(-128, 127)] * 64, [w1[oc * 64:oc * 64 + 64] for oc in range(128)],
                     b1, [spec["expand"][2]] * 128, relu=True, packed=True)
    # Residual skips from the centre bytes, reread raw and extracted per channel.
    w2 = signed_bytes(model, spec["contract"][0], 64 * 128)
    b2 = int32s(model, spec["contract"][1], 64)
    shifts, factors = spec["residual"]
    skip = skips(k, base, level, range(4), "x", "y")
    bias = [f"{b2[c]} + {skip[c]} * {factors[c]}" for c in range(64)]
    return k.layer(hidden, [(0, 127)] * 128, [w2[oc * 128:oc * 128 + 128] for oc in range(64)], bias, shifts,
                   packed=packed, block=block, done=done)


GENERATORS = {"down2x2": down2x2, "wide_block": wide_block, "wide_upsample": wide_upsample}
# Dispatch index -> pass description (model byte offsets, shapes and requantization shifts).
PASSES = {
    13: dict(name="pass6", kind="down2x2", cin=32, cout=64, weights=24576, bias=32768, shift=8,
             input=("RA", "Q"), output=("RB", "E")),
    15: dict(name="pass7", kind="wide_block", input=("RB", "E"), output=("RC", "E"),
             spatial=(33024, 37632, 7), expand=(37760, 45952, 7), contract=(46464, 54656),
             residual=([7] * 32 + [8] * 32, [128] * 64)),
    17: dict(name="pass8", kind="wide_block", input=("RC", "E"), output=("RB", "E"),
             spatial=(54912, 59520, 9), expand=(59648, 67840, 6), contract=(68352, 76544),
             residual=([8] * 64, [128] * 32 + [256] * 32)),
    19: dict(name="pass9", kind="wide_upsample", input=("RB", "E"), output=("RC", "Q"), skip=("RA", "Q"),
             spatial=(76800, 81408, 8), expand=(81536, 89728, 7), contract=(90240, 98432),
             residual=([7] * 64, [128] * 64), up=(98688, 106880, 9, 256), up_channels=32, extent_row=9),
}
BINDINGS = {"WEIGHTS": "model", "SCRATCH": "scratch", "CONSTANTS": "tensor"}


def generate(index, model, bindings):
    """GLSL source of the packed kernel replacing dispatch `index`, and its group count."""
    spec = PASSES[index]
    kernel = GENERATORS[spec["kind"]](model, spec)
    return kernel.source([(s, b, BINDINGS[role]) for s, b, _, role in bindings]), kernel.groups
