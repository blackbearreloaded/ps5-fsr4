# Copyright (C) 2026 BlackBearReloaded
# SPDX-License-Identifier: GPL-3.0-or-later
import struct
import unittest
from tools.fsr4_initializers import decode_case, inventory, RDATA_RVA, RDATA_RAW


class Initializers(unittest.TestCase):
    def test_instruction_bounds_and_provider_identity(self):
        # Synthetic case exercises signed RIP displacement without proprietary bytes.
        rva = RDATA_RVA + 0x400
        code = bytearray(bytes.fromhex(
            "b8 00 00 02 00 48 8d 0d 00 00 00 00 "
            "48 89 42 08 48 8b c2 48 89 0a 48 83 c4 18 c3"))
        struct.pack_into("<i", code, 8, RDATA_RVA - rva - 12)
        self.assertEqual(decode_case(code, rva), (RDATA_RAW, 131072))
        with self.assertRaises(ValueError):
            decode_case(code[:-1], rva)
        code[0] = 0
        with self.assertRaises(ValueError):
            decode_case(code, rva)
        code[0] = 0xB8
        struct.pack_into("<i", code, 8, RDATA_RVA - rva - 13)
        with self.assertRaises(ValueError):
            decode_case(code, rva)
        with self.assertRaisesRegex(ValueError, "identity mismatch"):
            inventory(b"not the pinned DLL")


if __name__ == "__main__":
    unittest.main()
