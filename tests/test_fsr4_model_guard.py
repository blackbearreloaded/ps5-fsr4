# Copyright (C) 2026 BlackBearReloaded
# SPDX-License-Identifier: GPL-3.0-or-later
"""The runtime builder makes the model vote and the first-layer test constant and removes what they left dead."""
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

# A guard in the shape of the model passes: each lane compares something it reads
# through a subgroup built-in, and the wave votes between the fast path and a fallback.
SOURCE = """
               OpCapability Shader
               OpCapability GroupNonUniform
               OpCapability GroupNonUniformVote
               OpMemoryModel Logical GLSL450
               OpEntryPoint GLCompute %main "main" %lane_id
               OpExecutionMode %main LocalSize 64 1 1
               OpDecorate %lane_id BuiltIn SubgroupLocalInvocationId
       %void = OpTypeVoid
       %func = OpTypeFunction %void
       %bool = OpTypeBool
       %uint = OpTypeInt 32 0
     %uint_0 = OpConstant %uint 0
   %subgroup = OpConstant %uint 3
   %ptr_uint = OpTypePointer Input %uint
    %lane_id = OpVariable %ptr_uint Input
       %main = OpFunction %void None %func
      %entry = OpLabel
       %lane = OpLoad %uint %lane_id
      %match = OpIEqual %bool %lane %uint_0
       %vote = OpGroupNonUniformAll %bool %subgroup %match
               OpSelectionMerge %merge None
               OpBranchConditional %vote %fast %merge
       %fast = OpLabel
               OpBranch %merge
      %merge = OpLabel
               OpReturn
               OpFunctionEnd
"""


# The generic body of a model pass serves workgroup layers z > 0, which the runtime never dispatches.
LAYERED = """
               OpCapability Shader
               OpMemoryModel Logical GLSL450
               OpEntryPoint GLCompute %main "main" %invocation
               OpExecutionMode %main LocalSize 64 1 1
               OpDecorate %invocation BuiltIn GlobalInvocationId
       %void = OpTypeVoid
       %func = OpTypeFunction %void
       %bool = OpTypeBool
       %uint = OpTypeInt 32 0
     %uint_0 = OpConstant %uint 0
     %uint_2 = OpConstant %uint 2
     %v3uint = OpTypeVector %uint 3
    %ptr_v3u = OpTypePointer Input %v3uint
   %ptr_uint = OpTypePointer Input %uint
 %invocation = OpVariable %ptr_v3u Input
       %main = OpFunction %void None %func
      %entry = OpLabel
     %z_ptr = OpAccessChain %ptr_uint %invocation %uint_2
          %z = OpLoad %uint %z_ptr
      %first = OpIEqual %bool %z %uint_0
               OpSelectionMerge %merge None
               OpBranchConditional %first %baked %generic
      %baked = OpLabel
               OpBranch %merge
    %generic = OpLabel
       %rest = OpIAdd %uint %z %uint_2
               OpBranch %merge
      %merge = OpLabel
               OpReturn
               OpFunctionEnd
"""


def tools_or_skip():
    tools = [shutil.which(name) for name in ("spirv-as", "spirv-val", "spirv-dis", "spirv-opt")]
    if not all(tools):
        raise unittest.SkipTest("SPIRV-Tools not installed")
    try:
        import build_fsr4_runtime
    except (ImportError, SystemExit) as error:
        raise unittest.SkipTest(f"runtime builder not importable: {error}")
    return tools, build_fsr4_runtime


def assemble(tools, directory, source):
    asm, spv = Path(directory) / "pass.spvasm", Path(directory) / "pass.spv"
    asm.write_text(source)
    subprocess.run([tools[0], "--target-env", "vulkan1.3", str(asm), "-o", str(spv)], check=True)
    return spv


def disassemble(tools, spv):
    return subprocess.run([tools[2], str(spv)], check=True, capture_output=True, text=True).stdout


class ModelGuard(unittest.TestCase):
    def test_vote_becomes_true(self):
        tools, builder = tools_or_skip()
        with tempfile.TemporaryDirectory() as tmp:
            spv = assemble(tools, tmp, SOURCE)
            code = spv.read_bytes()
            specialized = builder.specialize_model_guard(code)
            self.assertNotEqual(code, specialized)
            self.assertEqual(builder.specialize_model_guard(specialized), specialized)
            spv.write_bytes(specialized)
            subprocess.run([tools[1], "--target-env", "vulkan1.3", str(spv)], check=True)
            text = disassemble(tools, spv)
            self.assertNotIn("OpGroupNonUniformAll", text)
            self.assertIn("OpConstantTrue %bool", text)
            self.assertIn("= OpCopyObject %bool %true", text)
            self.assertIn("OpBranchConditional %true", text)

            stripped = builder.strip_dead_code(spv, True)
            text = disassemble(tools, spv)
            self.assertEqual(stripped, spv.read_bytes())
            self.assertNotIn("GroupNonUniform", text)
            self.assertNotIn("SubgroupLocalInvocationId", text)
            self.assertNotIn("OpBranchConditional", text)

    def test_dead_code_removal_keeps_live_work(self):
        tools, builder = tools_or_skip()
        with tempfile.TemporaryDirectory() as tmp:
            spv = assemble(tools, tmp, LAYERED)
            code = builder.specialize_single_layer(spv.read_bytes())
            spv.write_bytes(builder.remove_dead_code(code))
            subprocess.run([tools[1], "--target-env", "vulkan1.3", str(spv)], check=True)
            text = disassemble(tools, spv)
            self.assertNotIn("OpIAdd", text)            # the generic body's result was unused
            self.assertNotIn("OpIEqual", text)          # the branch now tests a constant
            self.assertIn("OpBranchConditional %true", text)

    def test_single_layer_drops_generic_body(self):
        tools, builder = tools_or_skip()
        with tempfile.TemporaryDirectory() as tmp:
            spv = assemble(tools, tmp, LAYERED)
            code = spv.read_bytes()
            specialized = builder.specialize_single_layer(code)
            self.assertNotEqual(code, specialized)
            self.assertEqual(builder.specialize_single_layer(specialized), specialized)
            spv.write_bytes(specialized)
            self.assertIn("OpBranchConditional %true", disassemble(tools, spv))
            builder.strip_dead_code(spv, False)
            text = disassemble(tools, spv)
            self.assertNotIn("OpBranchConditional", text)
            self.assertNotIn("OpIAdd", text)
            self.assertNotIn("GlobalInvocationId", text)


if __name__ == "__main__":
    unittest.main()
