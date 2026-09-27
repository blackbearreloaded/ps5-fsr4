# Copyright (C) 2026 BlackBearReloaded
# SPDX-License-Identifier: GPL-3.0-or-later
"""Integration check: contract fast FP32, preserve precise and FP16 operations.

Run after make fsr4-dxil-converter; uses the pinned DXC and SPIR-V tools.
"""
from pathlib import Path
import re
import os
import subprocess
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
    tools = ROOT / "build/runtime-graphics/toolchain/usr/bin"
    with tempfile.TemporaryDirectory(prefix="fsr4-fma-", dir=ROOT / "build") as temp:
        out = Path(temp)
        shader = out / "contract.hlsl"
        shader.write_text(SOURCE)
        for mode in range(5):
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
                if mode == 4:
                    # The round-trip shortcut must use the explicit scalar RTZ
                    # input, not quantize the unmasked FP32 input with RTE.
                    assert "OpQuantizeToF16" not in assembly, assembly
                    quant = re.findall(r"%\w+ = OpCopyObject %float (%\w+)", assembly)
                    assert len(quant) == 1, assembly
                    definitions = dict(re.findall(r"(%\w+) = ([^\n]+)", assembly))
                    pending, seen = quant[:], set()
                    while pending:
                        value = pending.pop()
                        if value in seen:
                            continue
                        seen.add(value)
                        pending.extend(re.findall(r"%\w+", definitions.get(value, "")))
                    assert any("OpShiftLeftLogical" in definitions.get(value, "")
                               for value in seen), assembly
                fused = re.findall(r"(%\w+) = OpExtInst %\w+ %\w+ Fma ", assembly)
                if mode == 0 and policy == "1":
                    assert len(fused) == 1, assembly
                    assert f"OpDecorate {fused[0]} NoContraction" in assembly
                else:
                    assert not fused, (mode, policy, assembly)
        invalid = subprocess.run([str(converter), "--self-test"],
                                 env=dict(os.environ, PS5_FSR4_FP32_FMA="typo"),
                                 capture_output=True, text=True)
        assert invalid.returncode == 2 and "must be 0 or 1" in invalid.stderr
    print("PASS: explicit/default FP32 policies; precise and FP16 boundaries; RTZ round trips; invalid policy rejected")


if __name__ == "__main__":
    main()
