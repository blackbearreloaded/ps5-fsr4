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
MAGIC = 12582912.0  # 1.5 * 2^23: FP32 values within 2^22 of it are spaced 1 apart
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
    """GLSL source under construction for one pass.

    With a `table` (a dword index into the model buffer), weight pairs are read from
    that table through the scalar cache instead of being encoded as 32-bit literals,
    which keeps each packed multiply-add at 8 bytes of code; `pairs` collects the
    table's entries in the order the kernel reads them.
    """

    def __init__(self, name, table=None):
        self.name = name
        self.lines = []
        self.counter = 0
        self.groups = 0
        self.table = table
        self.pairs = []

    def weight(self, w0, w1):
        """A weight pair: an inline constant when both halves are the same small value, else
        a literal or the next table entry."""
        if w0 == w1 and -16 <= w0 <= 64:
            return f"i16vec2(int16_t({w0}))"
        if self.table is None:
            return f"i16vec2(int16_t({w0}), int16_t({w1}))"
        self.pairs.append(((w1 & 0xffff) << 16) | (w0 & 0xffff))
        return f"i16vec2(unpack16(model_words[{self.table + len(self.pairs) - 1}u]))"

    def emit(self, line):
        self.lines.append("  " + line)

    def temp(self, prefix):
        self.counter += 1
        return f"{prefix}{self.counter}"

    def source(self, bindings):
        return "\n".join(self.preamble(bindings, 64) + ["void main() {"] + self.lines + ["}"]) + "\n"

    def function_source(self, bindings, signature, call):
        """GLSL of the body as function `signature`, with a main that only calls it so that it
        survives compilation (linking into another module drops that entry point)."""
        return "\n".join(self.preamble(bindings, 1) + [f"{signature} {{"] + self.lines +
                         ["}", "void main() {", f"  {call}", "}"]) + "\n"

    def preamble(self, bindings, local_size):
        head = ["#version 460",
                "#extension GL_EXT_shader_explicit_arithmetic_types_int16 : require",
                "#extension GL_EXT_control_flow_attributes : require",
                f"// {self.name}: generated from the local model, do not distribute",
                f"layout(local_size_x = {local_size}) in;"]
        for set_index, binding, kind in bindings:
            if kind == "model":
                head.append(f"layout(set = {set_index}, binding = {binding}) readonly restrict buffer Model "
                            "{ uint model_words[]; };")
            elif kind == "scratch":
                head.append(f"layout(set = {set_index}, binding = {binding}) buffer Scratch {{ uvec4 scratch[]; }};")
            elif kind == "tensor":
                head.append(f"layout(set = {set_index}, binding = {binding}) uniform Tensor {{ uvec4 tensor_rows[17]; }};")
        # Requantization: the FP32 adder rounds x * 2^-s + 1.5 * 2^23 half to even onto an
        # integer (exact for |x| < 2^22 * 2^s: the sum stays in [2^23, 2^24), where the ulp is
        # 1). After the INT8 clamp, the low 16 bits of its encoding are the result sign-extended,
        # and callers only use those bits (pack4 bytes, int16_t lanes).
        head += ["int requant(int x, float scale, float low) {",
                 f"  return floatBitsToInt(clamp(fma(float(x), scale, {MAGIC!r}), low, {MAGIC + 127!r}));",
                 "}",
                 "uint pack4(int a, int b, int c, int d) {",
                 "  return (uint(a) & 0xffu) | ((uint(b) & 0xffu) << 8) | ((uint(c) & 0xffu) << 16) | (uint(d) << 24);",
                 "}"]
        return head

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

    def load_bytes_banks(self, base, level, banks, x, y, inside=None):
        """Signed bytes of one pixel's 16-channel banks as i16 expressions; zero where the
        optional `inside` condition is false."""
        index = self.temp("at")
        self.emit(f"uint {index} = {self.tensor_index(base, level, x, y)};")
        values = []
        for bank in banks:
            vec = self.temp("v")
            load = f"scratch[{index} + {bank * self.plane(level)}u]"
            self.emit(f"uvec4 {vec} = {f'({inside}) ? {load} : uvec4(0u)' if inside else load};")
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
                    self.emit(f"{acc} {op} i16vec2({inputs[i]}) * {self.weight(rows[oc0][i], rows[oc1][i])};")
                self.emit(f"{sums[0]} += int({acc}.x); {sums[1]} += int({acc}.y);")
            results = [self.requant(s, shifts[oc], relu) for oc, s in zip((oc0, oc1), sums)]
            if packed:
                name = self.temp("p")
                self.emit(f"i16vec2 {name} = i16vec2(int16_t({results[0]}), int16_t({results[1]}));")
                out[oc0], out[oc1] = f"{name}.x", f"{name}.y"
            else:
                out[oc0], out[oc1] = results

    def requant(self, value, shift, relu=False):
        """The INT8 result of a sum: round-half-even shift, then saturation (ReLU before a
        rounding shift equals clamping the rounded value at zero). Only its low 16 bits are
        meaningful."""
        name = self.temp("q")
        self.emit(f"int {name} = requant({value}, {2.0 ** -shift!r}, {MAGIC if relu else MAGIC - 128!r});")
        return name

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
    k = Kernel(spec["name"], spec.get("table"))
    width, height = EXTENTS[spec["output"][1]]
    k.emit("uint x = gl_WorkGroupID.x * 64u + gl_LocalInvocationID.x;")
    k.emit("uint y = gl_WorkGroupID.y;")
    k.emit(f"if (x >= {width}u || y >= {height}u) return;")
    inputs = []
    for ky in range(2):
        for kx in range(2):
            inputs += k.load_bytes(spec["input"][0], spec["input"][1], cin, f"(2u * x + {kx}u)", f"(2u * y + {ky}u)")
    banks = spec.get("banks")
    if not banks:
        out = k.layer(inputs, [(-128, 127)] * len(inputs), rows, bias, [spec["shift"]] * cout)
        k.store_bytes(spec["output"][0], spec["output"][1], out, "x", "y")
        return k
    # One output bank per workgroup layer: a workgroup runs only its bank's code, so the
    # code the GPU runs at a time (the dispatcher walks z last) fits the instruction cache.
    per = cout // banks
    target = k.temp("to")
    k.emit(f"uint {target} = {k.tensor_index(spec['output'][0], spec['output'][1], 'x', 'y')};")
    for bank in range(banks):
        k.emit(f"{'if' if bank == 0 else '} else if'} (gl_WorkGroupID.z == {bank}u) {{")
        out = k.layer(inputs, [(-128, 127)] * len(inputs), rows[bank * per:(bank + 1) * per],
                      bias[bank * per:(bank + 1) * per], [spec["shift"]] * per)
        for part in range(per // 16):
            k.store_bank(target, spec["output"][1], bank * per // 16 + part, out[part * 16:(part + 1) * 16])
    k.emit("}")
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


def residual_block(model, spec):
    """Residual block stored to scratch at its own level (passes 1, 2, 4, 5, 7, 8, 10, 12)."""
    k = Kernel(spec["name"], spec.get("table"))
    level = spec["input"][1]
    start(k, level)
    target = k.temp("to")
    k.emit(f"uint {target} = {k.tensor_index(spec['output'][0], level, 'x', 'y')};")
    residual_prefix(k, model, spec, block=16,
                    done=lambda first, out: k.store_bank(target, level, first // 16, out[first:first + 16]))
    return k


def residual_upsample(model, spec):
    """Residual block followed by a learned 2x2 sub-pixel projection with a skip (passes 9
    and 11): each invocation writes the four sub-pixel phases of its position."""
    k = Kernel(spec["name"], spec.get("table"))
    start(k, spec["input"][1])
    prefix = residual_prefix(k, model, spec, packed=True)
    # Only positions inside the current tensor extent project; each phase stays inside the band.
    k.emit(f"uvec2 extent = tensor_rows[{spec['extent_row']}].xy;")
    k.emit("if (x >= extent.x || y >= extent.y) return;")
    offset, bias_offset, shift, factor = spec["up"]
    cin, cout = spec["channels"], spec["up_channels"]
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


def residual_prefix(k, model, spec, packed=False, block=None, done=None):
    """3x3 on channels 0-15 (0-31 as two groups of 16 in 64-channel blocks), concat of the
    remaining channels, 1x1 C->2C with ReLU, 1x1 2C->C and the per-channel residual
    requantization; returns the C outputs."""
    level, base, channels = spec["input"][1], spec["input"][0], spec["channels"]
    spatial = 32 if channels == 64 else 16
    w0 = signed_bytes(model, spec["spatial"][0], 9 * spatial * 16)
    b0 = int32s(model, spec["spatial"][1], spatial)
    if "pad_row" in spec:  # taps outside the current extent read zero instead of a cleared border
        k.emit(f"uvec2 pad_extent = {spec.get('pad_extent') or 'tensor_rows[%d].xy' % spec['pad_row']};")
    learned = []
    for g in range(spatial // 16):  # each spatial group reads only its own bank of the neighbourhood
        inputs = []
        for ky in range(3):
            for kx in range(3):
                tx, ty = f"(x + {kx}u - 1u)", f"(y + {ky}u - 1u)"
                inside = f"{tx} < pad_extent.x && {ty} < pad_extent.y" if "pad_row" in spec else None
                inputs += k.load_bytes_banks(base, level, (g,), tx, ty, inside)
        rows = [[w0[((ky * 3 + kx) * spatial + oc) * 16 + ic] for ky in range(3) for kx in range(3)
                 for ic in range(16)] for oc in range(g * 16, g * 16 + 16)]
        learned += k.layer(inputs, [(-128, 127)] * len(inputs), rows, b0[g * 16:g * 16 + 16],
                           [spec["spatial"][2]] * 16, packed=True)
    bypass = k.load_bytes_banks(base, level, range(spatial // 16, channels // 16), "x", "y")
    hidden_channels = 2 * channels
    w1 = signed_bytes(model, spec["expand"][0], hidden_channels * channels)
    b1 = int32s(model, spec["expand"][1], hidden_channels)
    hidden = k.layer(learned + bypass, [(-128, 127)] * channels,
                     [w1[oc * channels:(oc + 1) * channels] for oc in range(hidden_channels)],
                     b1, [spec["expand"][2]] * hidden_channels, relu=True, packed=True)
    # Residual skips from the centre bytes, reread raw and extracted per channel.
    w2 = signed_bytes(model, spec["contract"][0], channels * hidden_channels)
    b2 = int32s(model, spec["contract"][1], channels)
    shifts, factors = spec["residual"]
    skip = skips(k, base, level, range(channels // 16), "x", "y")
    bias = [f"{b2[c]} + {skip[c]} * {factors[c]}" for c in range(channels)]
    return k.layer(hidden, [(0, 127)] * hidden_channels,
                   [w2[oc * hidden_channels:(oc + 1) * hidden_channels] for oc in range(channels)],
                   bias, shifts, packed=packed, block=block, done=done)


GENERATORS = {"down2x2": down2x2, "residual_block": residual_block, "residual_upsample": residual_upsample}


def block(name, channels, io, spatial, expand, contract, residual):
    return dict(name=name, kind="residual_block", channels=channels, input=io[0], output=io[1],
                spatial=spatial, expand=expand, contract=contract, residual=residual)


# Dispatch index -> pass description (model byte offsets, shapes and requantization shifts).
PASSES = {
    3: block("pass1", 16, (("R0", "H"), ("RA", "H")), (1152, 3456, 7), (3584, 4096, 8), (4224, 4736),
             ([6] * 16, [64] * 16)),
    5: block("pass2", 16, (("RA", "H"), ("R0", "H")), (4864, 7168, 8), (7296, 7808, 7), (7936, 8448),
             ([7] * 16, [64] * 16)),
    9: block("pass4", 32, (("RA", "Q"), ("RB", "Q")), (10752, 13056, 7), (13184, 15232, 8), (15488, 17536),
             ([8] * 16 + [9] * 16, [256] * 32)),
    11: block("pass5", 32, (("RB", "Q"), ("RA", "Q")), (17664, 19968, 8), (20096, 22144, 8), (22400, 24448),
              ([8] * 32, [128] * 16 + [256] * 16)),
    13: dict(name="pass6", kind="down2x2", cin=32, cout=64, weights=24576, bias=32768, shift=8,
             input=("RA", "Q"), output=("RB", "E")),
    15: block("pass7", 64, (("RB", "E"), ("RC", "E")), (33024, 37632, 7), (37760, 45952, 7), (46464, 54656),
              ([7] * 32 + [8] * 32, [128] * 64)),
    17: block("pass8", 64, (("RC", "E"), ("RB", "E")), (54912, 59520, 9), (59648, 67840, 6), (68352, 76544),
              ([8] * 64, [128] * 32 + [256] * 32)),
    19: dict(block("pass9", 64, (("RB", "E"), ("RC", "Q")), (76800, 81408, 8), (81536, 89728, 7), (90240, 98432),
                   ([7] * 64, [128] * 64)),
             kind="residual_upsample", skip=("RA", "Q"), up=(98688, 106880, 9, 256), up_channels=32, extent_row=9),
    21: block("pass10", 32, (("RC", "Q"), ("RA", "Q")), (107008, 109312, 8), (109440, 111488, 7), (111744, 113792),
              ([7] * 32, [128] * 32)),
    23: dict(block("pass11", 32, (("RA", "Q"), ("RB", "H")), (113920, 116224, 8), (116352, 118400, 8),
                   (118656, 120704), ([9] * 32, [256] * 32)),
             kind="residual_upsample", skip=("R0", "H"), up=(120832, 122880, 7, 128), up_channels=16, extent_row=11),
    25: block("pass12", 16, (("RB", "H"), ("R0", "H")), (123008, 125312, 7), (125440, 125952, 8), (126080, 126592),
              ([7] * 16, [64] * 16)),
    # The postpass's learned head, run in place of the border clear that only it needed: its
    # 16-byte latent goes to the free RA region and the postpass reads it from there. Its taps
    # stop at the H tensor extent, which is rounded up from the output size, so it matches
    # the postpass only for output sizes that are multiples of 8; posthead_function (linked
    # into the postpass, the default) uses the postpass's own extent.
    26: dict(block("posthead", 16, (("R0", "H"), ("RA", "H")), (126720, 129024, 8), (129152, 129664, 7),
                   (129792, 130304), ([6] * 16, [64] * 16)), pad_row=13),
}
# The postpass reads its latent at the RA position of its H pixel (see route_postpass_latent).
POSTHEAD, POSTPASS = 26, 27
# Generated by default where the PS5 measured a gain (720p to 1080p demo, 3.46 -> 3.32 ms).
# Unrolled, passes 6-9 and 11 are bound by instruction fetch (their literal-heavy packed
# code is larger than the FP32 originals, whose ~230 distinct weights stay in SGPRs, or,
# for pass 11, than the instruction cache), so they run in loop form by default
# (3.30 -> 3.05 ms with wave64). The postpass head runs best inside the postpass, whose
# image loads it keeps busy: linked in as a function (posthead_function, 3.05 -> 2.90 ms),
# not split out into dispatch 26 (3.03 ms).
DEFAULT = (3, 5, 9, 11, 21, 23, 25)
# Passes with a loop form (fsr4_int8_loops): all but the posthead, whose taps check the extent.
LOOPED = tuple(index for index, spec in PASSES.items() if "pad_row" not in spec)
DEFAULT_LOOPS = (13, 15, 17, 19, 23)
BINDINGS = {"WEIGHTS": "model", "SCRATCH": "scratch", "CONSTANTS": "tensor"}


POSTHEAD_FUNCTION = "fsr4_posthead"


def posthead_function(model, scratch_binding):
    """GLSL source of the postpass head as `uvec4 fsr4_posthead(uint x, uint y, uint ext_x,
    uint ext_y)`: the latent's four packed words for H pixel (x, y). It is linked into the
    converted postpass in place of its FP32 head (build_fsr4_runtime.fuse_postpass_head);
    `scratch_binding` is (set, binding). Taps outside (ext_x, ext_y), the postpass's own
    extent, read zero: the cleared border cannot stand in for that check, because the
    tensor extent is rounded up to 8 and can reach beyond it."""
    spec = dict(PASSES[POSTHEAD], table=None, banks=None, pad_extent="uvec2(ext_x, ext_y)")
    k = Kernel(spec["name"] + " function")
    out = residual_prefix(k, model, spec)
    words = [f"pack4({', '.join(out[i:i + 4])})" for i in range(0, 16, 4)]
    k.emit(f"return uvec4({', '.join(words)});")
    signature = f"uvec4 {POSTHEAD_FUNCTION}(uint x, uint y, uint ext_x, uint ext_y)"
    call = f"scratch[0] = {POSTHEAD_FUNCTION}(0u, 0u, 0u, 0u);"
    return k.function_source([(scratch_binding[0], scratch_binding[1], "scratch")], signature, call), k.groups


def generate(index, model, bindings, table=None, banks=None, looped=False):
    """GLSL source of the packed kernel replacing dispatch `index`, its group count and, with a
    `table` dword index into the model buffer, the weight-pair words it reads from there.
    `banks` splits the outputs over that many workgroup layers (kinds that support it);
    `looped` generates the loop form (fsr4_int8_loops), which needs a table."""
    spec = dict(PASSES[index], table=table, banks=banks)
    if looped:
        import fsr4_int8_loops
        if table is None or banks:
            raise ValueError(f"looped kernel {index} needs a weight table and no banks")
        kernel = fsr4_int8_loops.GENERATORS[spec["kind"]](model, spec)
    else:
        kernel = GENERATORS[spec["kind"]](model, spec)
    return kernel.source([(s, b, BINDINGS[role]) for s, b, _, role in bindings]), kernel.groups, kernel.pairs
