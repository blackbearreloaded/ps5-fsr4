# Copyright (C) 2026 BlackBearReloaded
# SPDX-License-Identifier: GPL-3.0-or-later
"""Looped packed-i16 kernels for the E-level network passes.

Unrolled, the E-level passes (6-9) are bound by instruction fetch: every wave streams
100-150 KB of straight-line code through a 32 KB instruction cache. Here each layer
runs as a loop whose body fits the cache, and the body's weight pairs stream through
the scalar cache from a table after the model, one block of words per iteration:

  * the 3x3 layer loops over kernel rows (three taps per iteration),
  * the 1x1 expand and contract layers run fused, a few hidden-channel pairs per
    iteration, whose ReLU outputs feed the contract sums at once,
  * the 2x2 sub-pixel projection loops over its four phases,
  * the 2x2 downsampler loops over its four taps.

Group boundaries (where i16 lanes widen into i32 sums) are fixed in the body, so each
group is proven overflow-free for every iteration that runs it. The arithmetic is the
unrolled kernels', so both produce the same bytes.
"""
from fsr4_int8_kernels import (EXTENTS, I16_MAX, I16_MIN, Kernel, int32s, pair_channels, signed_bytes, skips,
                               start, step)

HIDDEN_PAIRS_PER_ITERATION = 4


class Stream:
    """Words a loop reads per iteration. The body names them by slot; closing the stream
    appends them to the kernel's table iteration-major and resolves the slot addresses."""

    def __init__(self, kernel, iterations, variable):
        self.kernel, self.iterations, self.variable = kernel, iterations, variable
        self.slots = []
        self.tag = f"@{kernel.temp('L')}@"

    def word(self, values):
        if len(values) != self.iterations:
            raise ValueError("a stream word needs one value per iteration")
        self.slots.append([v & 0xffffffff for v in values])
        return f"model_words[{self.tag}{len(self.slots) - 1}u]"

    def pair(self, pairs):
        return f"i16vec2(unpack16({self.word([((w1 & 0xffff) << 16) | (w0 & 0xffff) for w0, w1 in pairs])}))"

    def int(self, values):
        return f"int({self.word(values)})"

    def close(self):
        k = self.kernel
        base, stride = k.table + len(k.pairs), len(self.slots)
        k.pairs += [self.slots[s][i] for i in range(self.iterations) for s in range(stride)]
        k.lines = [line.replace(self.tag, f"{base}u + {self.variable} * {stride}u + ") for line in k.lines]


def group_iterations(rows, ranges):
    """Inputs of one channel pair split into groups whose i16 lanes cannot overflow in any
    iteration. rows[i] is iteration i's (row0, row1); ranges[j] bounds input j. First-fit
    decreasing on the largest lane step over the iterations; inputs whose weights are
    zero in every iteration are dropped."""
    items = []
    for j, (low, high) in enumerate(ranges):
        if any(r0[j] or r1[j] for r0, r1 in rows):
            steps = [(step(r0[j], low, high), step(r1[j], low, high)) for r0, r1 in rows]
            items.append((max(max(-a[0], a[1], -b[0], b[1]) for a, b in steps), j, steps))
    items.sort(key=lambda item: (-item[0], item[1]))
    groups = []
    for _, j, steps in items:
        for g in groups:
            grown = [(lo0 + a[0], hi0 + a[1], lo1 + b[0], hi1 + b[1])
                     for (lo0, hi0, lo1, hi1), (a, b) in zip(g["range"], steps)]
            if all(lo0 >= I16_MIN and hi0 <= I16_MAX and lo1 >= I16_MIN and hi1 <= I16_MAX
                   for lo0, hi0, lo1, hi1 in grown):
                g["range"] = grown
                g["inputs"].append(j)
                break
        else:
            groups.append({"range": [(a[0], a[1], b[0], b[1]) for a, b in steps], "inputs": [j]})
    return [sorted(g["inputs"]) for g in groups]


def accumulate(k, stream, inputs, rows, groups, sums):
    """sums[0], sums[1] += the channel pair's products over its groups, weights streamed."""
    acc = k.temp("g")
    k.emit(f"i16vec2 {acc};")
    for group in groups:
        k.groups += 1
        for n, j in enumerate(group):
            weight = stream.pair([(r0[j], r1[j]) for r0, r1 in rows])
            k.emit(f"{acc} {'=' if n == 0 else '+='} i16vec2({inputs[j]}) * {weight};")
        k.emit(f"{sums[0]} += int({acc}.x); {sums[1]} += int({acc}.y);")


def requantize(k, sums, shifts, relu=False):
    """Round-half-even shift and INT8 saturation of each sum; returns int expressions."""
    out = []
    for s, shift in zip(sums, shifts):
        name = k.temp("q")
        k.emit(f"int {name} = clamp(rne({s}, {shift}), {0 if relu else -128}, 127);")
        out.append(name)
    return out


def pack_pairs(k, values):
    """Consecutive int values packed as i16 pairs; returns one lane expression per value."""
    lanes = []
    for a, b in zip(values[::2], values[1::2]):
        name = k.temp("p")
        k.emit(f"i16vec2 {name} = i16vec2(int16_t({a}), int16_t({b}));")
        lanes += [f"{name}.x", f"{name}.y"]
    return lanes


def loop(k, iterations, name):
    variable = k.temp(name)
    k.emit(f"[[dont_unroll]] for (uint {variable} = 0u; {variable} < {iterations}u; {variable}++) {{")
    return variable, Stream(k, iterations, variable)


def spatial_rows(k, model, spec):
    """The 3x3 layer on channels 0-15 (0-31 as two groups of 16): one loop over kernel
    rows per group. Returns the learned channels as i16 lanes."""
    level, base, channels = spec["input"][1], spec["input"][0], spec["channels"]
    spatial = 32 if channels == 64 else 16
    w0 = signed_bytes(model, spec["spatial"][0], 9 * spatial * 16)
    b0 = int32s(model, spec["spatial"][1], spatial)

    def row(ky, oc):
        return [w0[((ky * 3 + kx) * spatial + oc) * 16 + ic] for kx in range(3) for ic in range(16)]

    learned = [None] * spatial
    for g in range(spatial // 16):
        full = [row(0, oc) + row(1, oc) + row(2, oc) for oc in range(g * 16, g * 16 + 16)]
        pairs = [(g * 16 + a, g * 16 + b) for a, b in pair_channels(full, -128, 127)]
        sums = {}
        for oc0, oc1 in pairs:
            sums[oc0], sums[oc1] = k.temp("s"), k.temp("s")
            k.emit(f"int {sums[oc0]} = {b0[oc0]}, {sums[oc1]} = {b0[oc1]};")
        ky, stream = loop(k, 3, "ky")
        inputs = []
        for kx in range(3):
            inputs += k.load_bytes_banks(base, level, (g,), f"(x + {kx}u - 1u)", f"(y + {ky} - 1u)")
        for oc0, oc1 in pairs:
            rows = [(row(r, oc0), row(r, oc1)) for r in range(3)]
            accumulate(k, stream, inputs, rows, group_iterations(rows, [(-128, 127)] * 48), (sums[oc0], sums[oc1]))
        k.emit("}")
        stream.close()
        for oc0, oc1 in pairs:
            q = requantize(k, (sums[oc0], sums[oc1]), [spec["spatial"][2]] * 2)
            learned[oc0], learned[oc1] = pack_pairs(k, q)
    return learned


def expand_contract(k, model, spec, inputs, sums):
    """The fused 1x1 C->2C (ReLU) and 2C->C layers: each iteration computes a few hidden
    pairs and adds them to the contract sums. Hidden pairs are ordered by weight mass and
    each body position takes a contiguous run of them, so a position's groups fit alike."""
    channels = spec["channels"]
    hidden = 2 * channels
    w1 = signed_bytes(model, spec["expand"][0], hidden * channels)
    b1 = int32s(model, spec["expand"][1], hidden)
    w2 = signed_bytes(model, spec["contract"][0], channels * hidden)
    rows1 = [w1[h * channels:(h + 1) * channels] for h in range(hidden)]
    rows2 = [w2[c * hidden:(c + 1) * hidden] for c in range(channels)]
    per = HIDDEN_PAIRS_PER_ITERATION
    hidden_pairs = pair_channels(rows1, -128, 127)
    iterations = len(hidden_pairs) // per
    at = [[hidden_pairs[j * iterations + i] for j in range(per)] for i in range(iterations)]
    _, stream = loop(k, iterations, "hp")
    lanes = []
    for j in range(per):
        rows = [(rows1[at[i][j][0]], rows1[at[i][j][1]]) for i in range(iterations)]
        s = (k.temp("s"), k.temp("s"))
        k.emit(f"int {s[0]} = {stream.int([b1[at[i][j][0]] for i in range(iterations)])}, "
               f"{s[1]} = {stream.int([b1[at[i][j][1]] for i in range(iterations)])};")
        accumulate(k, stream, inputs, rows, group_iterations(rows, [(-128, 127)] * channels), s)
        lanes += pack_pairs(k, requantize(k, s, [spec["expand"][2]] * 2, relu=True))
    for c0, c1 in pair_channels(rows2, 0, 127):
        rows = []
        for i in range(iterations):
            order = [h for j in range(per) for h in at[i][j]]
            rows.append(([rows2[c0][h] for h in order], [rows2[c1][h] for h in order]))
        accumulate(k, stream, lanes, rows, group_iterations(rows, [(0, 127)] * (2 * per)), (sums[c0], sums[c1]))
    k.emit("}")
    stream.close()


def residual_prefix(k, model, spec):
    """3x3 layer, concat, fused expand/contract and the residual requantization (C outputs)."""
    level, base, channels = spec["input"][1], spec["input"][0], spec["channels"]
    spatial = 32 if channels == 64 else 16
    learned = spatial_rows(k, model, spec)
    bypass = k.load_bytes_banks(base, level, range(spatial // 16, channels // 16), "x", "y")
    b2 = int32s(model, spec["contract"][1], channels)
    shifts, factors = spec["residual"]
    skip = skips(k, base, level, range(channels // 16), "x", "y")
    sums = [k.temp("t") for _ in range(channels)]
    for c in range(channels):
        k.emit(f"int {sums[c]} = {b2[c]} + {skip[c]} * {factors[c]};")
    expand_contract(k, model, spec, learned + bypass, sums)
    return requantize(k, sums, shifts)


def residual_block(model, spec):
    """Looped residual block stored to scratch at its own level (passes 7 and 8)."""
    k = Kernel(spec["name"], spec["table"])
    level = spec["input"][1]
    start(k, level)
    out = residual_prefix(k, model, spec)
    target = k.temp("to")
    k.emit(f"uint {target} = {k.tensor_index(spec['output'][0], level, 'x', 'y')};")
    for bank in range(len(out) // 16):
        k.store_bank(target, level, bank, out[bank * 16:(bank + 1) * 16])
    return k


def residual_upsample(model, spec):
    """Looped residual block and 2x2 sub-pixel projection with a skip (pass 9): the
    projection loops over the four phases, each writing one position of the next level."""
    k = Kernel(spec["name"], spec["table"])
    start(k, spec["input"][1])
    prefix = pack_pairs(k, residual_prefix(k, model, spec))
    k.emit(f"uvec2 extent = tensor_rows[{spec['extent_row']}].xy;")
    k.emit("if (x >= extent.x || y >= extent.y) return;")
    offset, bias_offset, shift, factor = spec["up"]
    cin, cout = spec["channels"], spec["up_channels"]
    w = signed_bytes(model, offset, 4 * cout * cin)
    bias = int32s(model, bias_offset, cout)
    out_base, out_level = spec["output"]
    width, height = EXTENTS[out_level]

    def row(phase, oc):
        return w[(phase * cout + oc) * cin:(phase * cout + oc + 1) * cin]

    pairs = pair_channels([sum((row(p, oc) for p in range(4)), []) for oc in range(cout)], -128, 127)
    phase, stream = loop(k, 4, "ph")
    qx, qy = k.temp("qx"), k.temp("qy")
    k.emit(f"uint {qx} = 2u * x + ({phase} & 1u), {qy} = 2u * y + ({phase} >> 1u);")
    k.emit(f"if ({qx} < {width}u && {qy} < {height}u) {{")
    skip = skips(k, spec["skip"][0], out_level, range(cout // 16), qx, qy)
    sums = [k.temp("u") for _ in range(cout)]
    for oc in range(cout):
        k.emit(f"int {sums[oc]} = {bias[oc]} + {skip[oc]} * {factor};")
    for o0, o1 in pairs:
        rows = [(row(p, o0), row(p, o1)) for p in range(4)]
        accumulate(k, stream, prefix, rows, group_iterations(rows, [(-128, 127)] * cin), (sums[o0], sums[o1]))
    out = requantize(k, sums, [shift] * cout)
    target = k.temp("to")
    k.emit(f"uint {target} = {k.tensor_index(out_base, out_level, qx, qy)};")
    for bank in range(cout // 16):
        k.store_bank(target, out_level, bank, out[bank * 16:(bank + 1) * 16])
    k.emit("}")
    k.emit("}")
    stream.close()
    return k


def down2x2(model, spec):
    """Looped learned 2x2 stride-2 downsampler (pass 6): one iteration per tap."""
    cin, cout = spec["cin"], spec["cout"]
    w = signed_bytes(model, spec["weights"], 4 * cout * cin)
    bias = int32s(model, spec["bias"], cout)
    k = Kernel(spec["name"], spec["table"])
    width, height = EXTENTS[spec["output"][1]]
    k.emit("uint x = gl_WorkGroupID.x * 64u + gl_LocalInvocationID.x;")
    k.emit("uint y = gl_WorkGroupID.y;")
    k.emit(f"if (x >= {width}u || y >= {height}u) return;")

    def row(t, oc):
        return [w[(t * cout + oc) * cin + ic] for ic in range(cin)]

    pairs = pair_channels([sum((row(t, oc) for t in range(4)), []) for oc in range(cout)], -128, 127)
    sums = [k.temp("s") for _ in range(cout)]
    for oc in range(cout):
        k.emit(f"int {sums[oc]} = {bias[oc]};")
    tap, stream = loop(k, 4, "tap")
    inputs = k.load_bytes(spec["input"][0], spec["input"][1], cin, f"(2u * x + ({tap} & 1u))", f"(2u * y + ({tap} >> 1u))")
    for o0, o1 in pairs:
        rows = [(row(t, o0), row(t, o1)) for t in range(4)]
        accumulate(k, stream, inputs, rows, group_iterations(rows, [(-128, 127)] * cin), (sums[o0], sums[o1]))
    k.emit("}")
    stream.close()
    k.store_bytes(spec["output"][0], spec["output"][1], requantize(k, sums, [spec["shift"]] * cout), "x", "y")
    return k


GENERATORS = {"down2x2": down2x2, "residual_block": residual_block, "residual_upsample": residual_upsample}
