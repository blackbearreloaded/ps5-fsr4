# Copyright (C) 2026 BlackBearReloaded
# SPDX-License-Identifier: GPL-3.0-or-later
"""Check DXIL container bounds before accepting provider metadata."""
import struct
import unittest

from tools.fsr4_provider_inventory import container, scan


class ProviderInventoryTest(unittest.TestCase):
    def test_container_scan_and_truncation(self):
        name = b"fsr4_model_v07_fp8_no_scale_pass0_post"
        blob = bytearray(44 + len(name))
        blob[:4] = b"DXBC"
        struct.pack_into("<III", blob, 20, 1, len(blob), 1)
        struct.pack_into("<I", blob, 32, 36)
        blob[36:40] = b"DXIL"
        struct.pack_into("<I", blob, 40, len(name))
        blob[44:] = name
        self.assertEqual(container(blob, 0)["embedded_model_entry"], name.decode())
        self.assertEqual([r["offset"] for r in scan(b"noise" + blob + blob)],
                         [5, 5 + len(blob)])
        self.assertEqual(container(blob[:-1], 0), None)
        struct.pack_into("<I", blob, 40, 99)
        self.assertEqual(container(blob, 0), None)


if __name__ == "__main__":
    unittest.main()
