# Copyright (C) 2026 BlackBearReloaded
# SPDX-License-Identifier: GPL-3.0-or-later
"""The runtime builder turns the matching-model wave vote into a constant and removes what it left dead."""
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


class ModelGuard(unittest.TestCase):
    def test_vote_becomes_true(self):
        tools = [shutil.which(name) for name in ("spirv-as", "spirv-val", "spirv-dis", "spirv-opt")]
        if not all(tools):
            raise unittest.SkipTest("SPIRV-Tools not installed")
        try:
            from build_fsr4_runtime import specialize_model_guard, strip_model_guard
        except (ImportError, SystemExit) as error:
            raise unittest.SkipTest(f"runtime builder not importable: {error}")
        with tempfile.TemporaryDirectory() as tmp:
            asm, spv = Path(tmp) / "guard.spvasm", Path(tmp) / "guard.spv"
            asm.write_text(SOURCE)
            subprocess.run([tools[0], "--target-env", "vulkan1.3", str(asm), "-o", str(spv)], check=True)
            code = spv.read_bytes()
            specialized = specialize_model_guard(code)
            self.assertNotEqual(code, specialized)
            self.assertEqual(specialize_model_guard(specialized), specialized)
            spv.write_bytes(specialized)
            subprocess.run([tools[1], "--target-env", "vulkan1.3", str(spv)], check=True)
            text = subprocess.run([tools[2], str(spv)], check=True, capture_output=True, text=True).stdout
            self.assertNotIn("OpGroupNonUniformAll", text)
            self.assertIn("OpConstantTrue %bool", text)
            self.assertIn("= OpCopyObject %bool %true", text)
            self.assertIn("OpBranchConditional %true", text)

            stripped = strip_model_guard(spv)
            text = subprocess.run([tools[2], str(spv)], check=True, capture_output=True, text=True).stdout
            self.assertEqual(stripped, spv.read_bytes())
            self.assertNotIn("GroupNonUniform", text)
            self.assertNotIn("SubgroupLocalInvocationId", text)
            self.assertNotIn("OpBranchConditional", text)


if __name__ == "__main__":
    unittest.main()
