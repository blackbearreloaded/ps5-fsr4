# Copyright (C) 2026 BlackBearReloaded
# SPDX-License-Identifier: GPL-3.0-or-later
"""Integration check: contract fast FP32, preserve precise and FP16 operations.

Run after make fsr4-dxil-converter; uses the pinned DXC and SPIR-V tools.
"""
from pathlib import Path
import re
import os
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SOURCE = """
ByteAddressBuffer Input : register(t0);
RWByteAddressBuffer Output : register(u0);
[numthreads(1,1,1)]
void main(uint3 tid : SV_DispatchThreadID) {
    float3 v = asfloat(Input.Load3(tid.x * 12));
#if MODE == 0
    float value = v.x * v.y + v.z;
#elif MODE == 1
    precise float value = v.x * v.y + v.z;
#elif MODE == 2
    half value = (half)v.x * (half)v.y + (half)v.z;
#elif MODE == 4
    float value = (float)(half)v.x;
#elif MODE == 5
    float value = dot2add(half2(v.x, v.y), half2(v.z, v.x), v.y);
#elif MODE == 6
    float value = dot2add(half2(v.x, v.y), half2(v.z, 0), v.y);
#else
    precise float product = v.x * v.y;
    float value = product + v.z;
#endif
    Output.Store(tid.x * 4, asuint((float)value));
}
"""


def main():
    dxc = ROOT / "build/reference-runtime/dxc/linux_dxc_2026_07_29.x86_x64/bin/dxc"
    converter = ROOT / "build/fsr4_dxil_to_spirv"
    sys.path.insert(0, str(ROOT / "tools"))
    from fsr4_paths import TOOLCHAIN_BIN as tools
    with tempfile.TemporaryDirectory(prefix="fsr4-fma-", dir=ROOT / "build") as temp:
        out = Path(temp)
        shader = out / "contract.hlsl"
        shader.write_text(SOURCE)
        for mode in range(7):
            dxil, spv = out / f"{mode}.dxil", out / f"{mode}.spv"
            subprocess.run([str(dxc), "-T", "cs_6_6", "-E", "main",
                            "-enable-16bit-types", "-D", f"MODE={mode}",
                            "-Fo", str(dxil), str(shader)], check=True)
            for policy in ("0", "1"):
                env = dict(os.environ, PS5_FSR4_FP32_FMA=policy)
                subprocess.run([str(converter), str(dxil), str(spv)], env=env, check=True)
                subprocess.run([str(tools / "spirv-val"), "--target-env",
                                "vulkan1.3", str(spv)], check=True)
                assembly = subprocess.check_output([str(tools / "spirv-dis"), str(spv)], text=True)
                definitions = dict(re.findall(r"(%\w+) = ([^\n]+)", assembly))
                exact = set(re.findall(r"OpDecorate (%\w+) NoContraction", assembly))
                # Every FP16 -> FP32 extension is exact, so compilers cannot fold
                # it with a preceding conversion and keep FP32 precision.
                for value, definition in definitions.items():
                    source = re.match(r"OpFConvert %float (%\w+)$", definition)
                    if source and definitions.get(source.group(1), "").startswith(("OpFConvert %half", "OpBitcast %half")):
                        assert value in exact, (mode, value, assembly)
                if "OpCapability Float16" in assembly:
                    assert "OpExecutionMode %main RoundingModeRTE 16" in assembly, assembly
                if mode == 4:
                    # The round-trip shortcut rounds toward zero explicitly: the
                    # nearest-even result steps toward zero where it grew.
                    assert "OpQuantizeToF16" not in assembly, assembly
                    quant = re.findall(r"%\w+ = OpCopyObject %float (%\w+)", assembly)
                    assert len(quant) == 1, assembly
                    pending, seen = quant[:], set()
                    while pending:
                        value = pending.pop()
                        if value in seen:
                            continue
                        seen.add(value)
                        pending.extend(re.findall(r"%\w+", definitions.get(value, "")))
                    chain = [definitions.get(value, "") for value in seen]
                    assert any(d.startswith("OpFOrdGreaterThan") for d in chain), assembly
                    assert any(d.startswith("OpISub %ushort") for d in chain), assembly
                fused = re.findall(r"(%\w+) = OpExtInst %\w+ %\w+ Fma ", assembly)
                if mode in (5, 6):
                    # Exact half products fuse into the FP32 accumulation; a
                    # constant zero factor drops its product.
                    assert len(fused) == (2 if mode == 5 else 1), assembly
                    assert all(value in exact for value in fused), assembly
                elif mode == 0 and policy == "1":
                    assert len(fused) == 1, assembly
                    assert f"OpDecorate {fused[0]} NoContraction" in assembly
                else:
                    assert not fused, (mode, policy, assembly)
        invalid = subprocess.run([str(converter), "--self-test"],
                                 env=dict(os.environ, PS5_FSR4_FP32_FMA="typo"),
                                 capture_output=True, text=True)
        assert invalid.returncode == 2 and "must be 0 or 1" in invalid.stderr
    print("PASS: explicit/default FP32 policies; precise and FP16 boundaries; RTZ round trips; "
          "exact FP16 extensions; fused dot2add; invalid policy rejected")


if __name__ == "__main__":
    main()
