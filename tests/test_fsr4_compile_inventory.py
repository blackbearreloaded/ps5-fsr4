# Copyright (C) 2026 BlackBearReloaded
# SPDX-License-Identifier: GPL-3.0-or-later
"""One check for the FSR4 descriptor inventory parser."""
import unittest
from tools.fsr4_compile_inventory import bindings


class InventoryTest(unittest.TestCase):
    def test_separate_buffer_image_and_sampler(self):
        text = """
OpDecorate %a DescriptorSet 2
OpDecorate %a Binding 0
OpDecorate %b DescriptorSet 1
OpDecorate %b Binding 3
OpDecorate %c DescriptorSet 3
OpDecorate %c Binding 0
OpDecorate %d DescriptorSet 1
OpDecorate %d Binding 2
%img_sampled = OpTypeImage %float 2D 0 0 0 1 Unknown
%arr_sampled = OpTypeRuntimeArray %img_sampled
%ptr_sampled = OpTypePointer UniformConstant %arr_sampled
%img = OpTypeImage %float 2D 0 0 0 2 Unknown
%arr = OpTypeRuntimeArray %img
%ptr = OpTypePointer UniformConstant %arr
%sptr = OpTypePointer UniformConstant %sampler
%sampler = OpTypeSampler
%a = OpVariable %buffer StorageBuffer
%b = OpVariable %ptr UniformConstant
%c = OpVariable %sptr UniformConstant
%d = OpVariable %ptr_sampled UniformConstant
"""
        self.assertEqual(bindings(text), [
            (1, 2, "sampled-image", 64),
            (1, 3, "storage-image", 64),
            (2, 0, "buffer", 64),
            (3, 0, "sampler", 1),
        ])


if __name__ == "__main__":
    unittest.main()
