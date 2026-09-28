#!/usr/bin/env python3
"""Build native FP32 -> FP16 boundary witnesses with independent RTZ/RTE oracles."""
import argparse
import bisect
import hashlib
import json
import math
from pathlib import Path
import struct
import subprocess
import zlib
from build_fsr4_clear import ROOT, DIST_SDK, build_native_app, native_inputs
from fsr4_paths import TOOLCHAIN_BIN
from build_fsr4_frame import FRAME_DECLARATIONS, sha


def corpus(rounding="rtz"):
    if rounding not in ("rtz", "rte"):
        raise ValueError("Unknown FP16 rounding mode")
    # All finite positive half values and adjacent FP32 values; the expected
    # result is a lookup in the ordered representable set, not bit-mask math.
    halves = [struct.unpack("<e", struct.pack("<H", bits))[0] for bits in range(0x7c00)]
    inputs = {0, 1, 0x7fffff, 0x800000, 0x477fffff, 0x47800000,
              0x7f7fffff, 0x7f800000, 0x7fc00000, 0x7f800001}
    for value in halves:
        bits = struct.unpack("<I", struct.pack("<f", value))[0]
        inputs.update([bits, bits + 1])
        if bits: inputs.add(bits - 1)
    if rounding == "rte":
        # Every midpoint and its FP32 neighbors exercise ties-to-even; include
        # the finite-to-infinity boundary as well as the subnormal interval.
        for left, right in zip(halves, halves[1:] + [65536.0]):
            midpoint = struct.unpack("<I", struct.pack("<f", (left + right) / 2))[0]
            inputs.update([midpoint - 1, midpoint, midpoint + 1])
    packed, expected = bytearray(), bytearray()
    for bits in sorted(inputs):
        value = struct.unpack("<f", struct.pack("<I", bits))[0]
        half = (0x7e00 if math.isnan(value) else 0x7c00 if math.isinf(value)
                else max(0, bisect.bisect_right(halves, value) - 1))
        if rounding == "rte" and math.isfinite(value):
            try:
                half = struct.unpack("<H", struct.pack("<e", value))[0]
            except OverflowError:
                half = 0x7c00
        for sign in (0, 0x80000000):
            packed += struct.pack("<I", bits | sign)
            expected += struct.pack("<I", half | (sign >> 16))
    return bytes(packed), bytes(expected)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=ROOT / "build/fsr4-precision-app")
    parser.add_argument("--rounding", choices=["rtz", "rte"], default="rtz")
    args = parser.parse_args()
    native_inputs()
    out = args.out.resolve()
    package = out / "PPSA88900"
    assets = package / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    data, expected = corpus(args.rounding)
    count = len(data) // 4
    page_bytes = 192 * 144 * 4 if args.rounding == "rte" else len(data)
    resources, pages = [], []
    for base in range(0, len(data), page_bytes):
        chunk = data[base:base + page_bytes].ljust(page_bytes, b"\0")
        oracle = expected[base:base + page_bytes].ljust(page_bytes, b"\0")
        index = len(resources)
        (assets / f"input-{index}.bin").write_bytes(chunk)
        (assets / f"input-{index+1}.bin").write_bytes(bytes(page_bytes))
        (assets / f"expected-{index+1}.bin").write_bytes(oracle)
        fmt = "VK_FORMAT_R32_SFLOAT" if args.rounding == "rte" else "VK_FORMAT_UNDEFINED"
        width, height = (192, 144) if args.rounding == "rte" else (0, 0)
        usage = "VK_IMAGE_USAGE_SAMPLED_BIT|VK_IMAGE_USAGE_TRANSFER_DST_BIT" if width else "0"
        resources += [(index, "", fmt, width, height, page_bytes, usage),
                      (index+1, f"expected-{index+1}.bin", "VK_FORMAT_UNDEFINED", 0, 0, page_bytes, "0")]
        pages.append((index, zlib.crc32(oracle)))
    subprocess.run(["make", "fsr4-dxil-converter"], cwd=ROOT, check=True)
    dxc = ROOT / "build/reference-runtime/dxc/linux_dxc_2026_07_29.x86_x64/bin/dxc"
    dxil, spv = out / "precision.dxil", out / "precision.spv"
    subprocess.run([str(dxc), "-T", "cs_6_6", "-E", "main", "-enable-16bit-types",
                    "-D", f"COUNT={page_bytes//4}", "-D", f"ROUND_TO_EVEN={int(args.rounding == 'rte')}", "-Fo", str(dxil),
                    str(ROOT / "experiments/fp16_rtz.hlsl")], check=True)
    subprocess.run([str(ROOT / "build/fsr4_dxil_to_spirv"), str(dxil), str(spv)], check=True)
    subprocess.run([str(TOOLCHAIN_BIN / "spirv-val"),
                    "--target-env", "vulkan1.3", str(spv)], check=True)
    code = spv.read_bytes()
    fixture_id = sha(data + expected + code)
    header = FRAME_DECLARATIONS
    header += 'static const struct fsr4_resource_desc fsr4_resources[] = {\n'
    for n, expected_name, fmt, width, height, size, usage in resources:
        header += '{"input-%d.bin","%s",%s,%d,%d,%d,0,%s},\n' % (n, expected_name, fmt, width, height, size, usage)
    header += '};\nstatic const uint32_t code0[] = {' + ",".join(hex(w) for w in struct.unpack(f"<{len(code)//4}I", code)) + '};\n'
    kind = "VK_DESCRIPTOR_TYPE_SAMPLED_IMAGE" if args.rounding == "rte" else "VK_DESCRIPTOR_TYPE_STORAGE_BUFFER"
    for n, crc in pages:
        header += 'static const struct fsr4_binding bindings%d[] = {{0,0,%s,%d},{1,0,VK_DESCRIPTOR_TYPE_STORAGE_BUFFER,%d}};\n' % (n, kind, n, n+1)
        header += 'static const struct fsr4_output outputs%d[] = {{%d,0x%08xu}};\n' % (n, n+1, crc)
    header += 'static const struct fsr4_pass fsr4_passes[] = {\n'
    for n, crc in pages:
        header += '{%d,{%d,1,1},code0,sizeof(code0),bindings%d,2,outputs%d,1},\n' % (n//2, (page_bytes//4+63)//64, n, n)
    header += '};\n'
    header += '#define FSR4_FIXTURE_ID "%s"\n' % fixture_id
    (out / "fsr4_frame_fixture.h").write_text(header)
    build_native_app(out, ROOT / "examples/fsr4_frame_main.c", "FSR4 Precision Test")
    manifest = dict(title="PPSA88900", fixture_id=fixture_id, test=("typed texture FP32 to FP16 RTE" if args.rounding == "rte" else "scalar FP32 to FP16 RTZ"),
                    cases=count, expected_sha256=sha(expected), dxil_sha256=sha(dxil.read_bytes()),
                    spirv_sha256=sha(code), hardware_tested=False, complete_fsr4=False,
                    converter_patch_sha256=sha((ROOT / "tools/dxil-spirv-fsr4-fp16.patch").read_bytes()),
                    sdk_archive_sha256=sha((DIST_SDK / "lib/libps5vk.a").read_bytes()),
                    files={str(p.relative_to(package)):sha(p.read_bytes()) for p in sorted(package.rglob("*")) if p.is_file()})
    (out / "artifact.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(dict(package=str(package), cases=count, fixture_id=fixture_id), indent=2))


if __name__ == "__main__":
    main()
