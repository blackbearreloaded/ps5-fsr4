# Copyright (C) 2026 BlackBearReloaded
# SPDX-License-Identifier: GPL-3.0-or-later
import struct
import unittest
from tools.fsr4_scalar_unpack import lower


class ScalarUnpack(unittest.TestCase):
    def test_both_lanes_and_sign_bits_are_preserved(self):
        # Exhaust both halfword domains, including negative signed i16 values.
        for half in range(65536):
            word = (half << 16) | (half ^ 0xa55a)
            lo, hi = word & 65535, (word >> 16) & 65535
            self.assertEqual(struct.pack("<HH", lo, hi), struct.pack("<I", word))
        source = "  %v840 = bitcast i32 %v837 to <2 x i16>\n  %next = add i32 %v837, 1\n"
        result, count = lower(source)
        self.assertEqual(count, 1)
        self.assertIn("lshr i32 %v837, 16", result)
        self.assertIn("%v840 = insertelement", result)
        self.assertTrue(result.endswith("  %next = add i32 %v837, 1\n"))
        self.assertNotIn("bitcast", result)
        with self.assertRaises(ValueError):
            lower(result)


if __name__ == "__main__":
    unittest.main()
