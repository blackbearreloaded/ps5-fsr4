# Copyright (C) 2026 BlackBearReloaded
# SPDX-License-Identifier: GPL-3.0-or-later
"""The runtime builder links the generated INT8 head into a postpass in place of its FP32 head."""
import random
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

# The shape the builder looks for: an H pixel (x, y) skipped outside the extent, the first
# scratch read at dword (15392 * y + 16 * x) / 4, and sixteen SClamps of which the last
# four are packed into the words the rest of the pass uses.
POSTPASS = """
               OpCapability Shader
               OpCapability Int8
               OpMemoryModel Logical GLSL450
       %glsl = OpExtInstImport "GLSL.std.450"
               OpEntryPoint GLCompute %main "main" %wgid
               OpExecutionMode %main LocalSize 256 1 1
               OpDecorate %wgid BuiltIn WorkgroupId
               OpDecorate %scratch DescriptorSet 1
               OpDecorate %scratch Binding 11
               OpDecorate %scratch NonWritable
               OpDecorate %result DescriptorSet 1
               OpDecorate %result Binding 2
               OpDecorate %words ArrayStride 4
               OpMemberDecorate %block 0 Offset 0
               OpDecorate %block Block
       %void = OpTypeVoid
       %func = OpTypeFunction %void
       %bool = OpTypeBool
       %uint = OpTypeInt 32 0
        %int = OpTypeInt 32 1
      %uchar = OpTypeInt 8 0
     %v3uint = OpTypeVector %uint 3
     %v4uint = OpTypeVector %uint 4
      %v4int = OpTypeVector %int 4
    %v4uchar = OpTypeVector %uchar 4
      %words = OpTypeRuntimeArray %uint
      %block = OpTypeStruct %words
    %ptr_buf = OpTypePointer StorageBuffer %block
   %ptr_word = OpTypePointer StorageBuffer %uint
     %ptr_in = OpTypePointer Input %v3uint
%ptr_in_uint = OpTypePointer Input %uint
    %scratch = OpVariable %ptr_buf StorageBuffer
     %result = OpVariable %ptr_buf StorageBuffer
       %wgid = OpVariable %ptr_in Input
     %uint_0 = OpConstant %uint 0
     %uint_1 = OpConstant %uint 1
     %uint_2 = OpConstant %uint 2
     %uint_3 = OpConstant %uint 3
     %uint_4 = OpConstant %uint 4
   %uint_row = OpConstant %uint 15392
   %uint_960 = OpConstant %uint 960
   %uint_540 = OpConstant %uint 540
     %int_lo = OpConstant %int -128
     %int_hi = OpConstant %int 127
        %lo4 = OpConstantComposite %v4int %int_lo %int_lo %int_lo %int_lo
        %hi4 = OpConstantComposite %v4int %int_hi %int_hi %int_hi %int_hi
       %main = OpFunction %void None %func
      %entry = OpLabel
         %xp = OpAccessChain %ptr_in_uint %wgid %uint_0
          %x = OpLoad %uint %xp
         %yp = OpAccessChain %ptr_in_uint %wgid %uint_1
          %y = OpLoad %uint %yp
      %out_x = OpUGreaterThanEqual %bool %x %uint_960
      %out_y = OpUGreaterThanEqual %bool %y %uint_540
    %outside = OpLogicalOr %bool %out_x %out_y
               OpSelectionMerge %done None
               OpBranchConditional %outside %done %head
       %head = OpLabel
      %shift = OpShiftLeftLogical %uint %x %uint_4
        %row = OpIMul %uint %uint_row %y
      %bytes = OpIAdd %uint %row %shift
      %index = OpShiftRightLogical %uint %bytes %uint_2
        %tap = OpAccessChain %ptr_word %scratch %uint_0 %index
      %value = OpLoad %uint %tap
          %v = OpCompositeConstruct %v4uint %value %value %value %value
         %vi = OpBitcast %v4int %v
{clamps}
       %slot = OpIMul %uint %x %uint_4
{stores}
               OpBranch %done
       %done = OpLabel
               OpReturn
               OpFunctionEnd
"""


def postpass_source():
    lines, previous = [], "%vi"
    for n in range(12):
        lines.append(f"        %c{n} = OpExtInst %v4int %glsl SClamp {previous} %lo4 %hi4")
        previous = f"%c{n}"
    stores = []
    for n in range(4):
        lines += [f"        %f{n} = OpExtInst %v4int %glsl SClamp {previous} %lo4 %hi4",
                  f"        %u{n} = OpUConvert %v4uchar %f{n}",
                  f"        %p{n} = OpBitcast %uint %u{n}"]
        stores += [f"        %s{n} = OpIAdd %uint %slot %uint_{n}",
                   f"        %o{n} = OpAccessChain %ptr_word %result %uint_0 %s{n}",
                   f"                OpStore %o{n} %p{n}"]
    return POSTPASS.replace("{clamps}", "\n".join(lines)).replace("{stores}", "\n".join(stores))


class PostpassHead(unittest.TestCase):
    def test_int8_head_replaces_the_fp32_head(self):
        tools = [shutil.which(name) for name in ("spirv-as", "spirv-dis", "spirv-link", "glslangValidator")]
        if not all(tools):
            raise unittest.SkipTest("SPIRV-Tools with spirv-link and glslang not installed")
        try:
            import build_fsr4_runtime as builder
        except (ImportError, SystemExit) as error:
            raise unittest.SkipTest(f"runtime builder not importable: {error}")
        rng = random.Random(5)
        model = bytes(rng.getrandbits(8) for _ in range(131072))
        with tempfile.TemporaryDirectory() as tmp:
            asm, spv = Path(tmp) / "postpass.spvasm", Path(tmp) / "postpass.spv"
            asm.write_text(postpass_source())
            subprocess.run([tools[0], "--target-env", "vulkan1.1", str(asm), "-o", str(spv)], check=True)
            code = builder.fuse_postpass_head(spv.read_bytes(), model, (1, 11), Path(tmp))
            fused = Path(tmp) / "fused.spv"
            fused.write_bytes(code)
            text = subprocess.run([tools[1], str(fused)], check=True, capture_output=True, text=True).stdout
        self.assertNotIn("SClamp", text)                   # the FP32 head is gone
        self.assertNotIn("Linkage", text)
        self.assertNotIn("WorkgroupSize", text)            # the head's own size does not override
        self.assertIn("LocalSize 256 1 1", text)
        self.assertNotIn("OpFunctionCall", text)           # inlined
        self.assertIn("OpIMul %v2short", text)             # packed i16 multiply-adds


if __name__ == "__main__":
    unittest.main()
