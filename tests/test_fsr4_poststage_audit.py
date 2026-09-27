# Copyright (C) 2026 BlackBearReloaded
# SPDX-License-Identifier: GPL-3.0-or-later
"""A changed post-stage write must fail the static clear audit."""
import unittest

from tools.fsr4_poststage_audit import summarize_ir


class PoststageAuditTest(unittest.TestCase):
    def test_zero_store_only(self):
        entry = "fsr4_model_v07_fp8_no_scale_pass0_post"
        body = f"""define void @{entry}() {{
  %1 = call i32 @dx.op.threadId.i32(i32 93, i32 0)
  %2 = call %ret @dx.op.cbufferLoadLegacy.i32(i32 59, %handle %0, i32 0)
  call void @dx.op.rawBufferStore.i32(i32 140, %handle %0, i32 %1, i32 undef, i32 0, i32 0, i32 0, i32 0, i8 15, i32 4)
  ret void
}}
"""
        self.assertEqual(summarize_ir(body, entry)["zero_stores"], 1)
        with self.assertRaises(ValueError):
            summarize_ir(body.replace("i32 0, i32 0, i32 0, i32 0, i8 15",
                                      "i32 1, i32 0, i32 0, i32 0, i8 15"), entry)


if __name__ == "__main__":
    unittest.main()
