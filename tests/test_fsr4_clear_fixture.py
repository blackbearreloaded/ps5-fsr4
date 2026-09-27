# Copyright (C) 2026 BlackBearReloaded
# SPDX-License-Identifier: GPL-3.0-or-later
import unittest
from unittest.mock import patch
from tools.build_fsr4_clear import fixture, padding_offsets, SCRATCH_BYTES, buffer_chain, sha


class ClearFixture(unittest.TestCase):
    def test_connected_fixture_rejects_intermediate_tensor_changes(self):
        passes = [
            dict(shader={"sha256": "a" * 64}, srv=[], cbv=[], dispatchDimension=[1, 1, 1],
                 uav=[dict(descriptor=dict(resource="ResourceId::295", byteOffset=0,
                                           byteSize=SCRATCH_BYTES),
                           before_event=dict(sha256=f"{i:064x}", bytes=SCRATCH_BYTES),
                           at_event=dict(sha256=f"{i + 1:064x}", bytes=SCRATCH_BYTES))])
            for i in range(25)]
        graph = {"dispatches": [{}, {}, *passes]}
        with patch("tools.build_fsr4_clear.CHAIN_SHADER_ID", sha(("a" * 64 * 25).encode())):
            self.assertEqual(len(buffer_chain(graph)), 25)
            passes[10]["uav"][0]["before_event"]["sha256"] = "b" * 64
            with self.assertRaisesRegex(ValueError, "changed between"):
                buffer_chain(graph)

    def test_exact_padding_and_untouched_interior(self):
        before, expected = fixture()
        self.assertEqual(len(before), SCRATCH_BYTES)
        self.assertEqual(before.count(b"\xa5"), SCRATCH_BYTES)
        self.assertEqual(expected.count(b"\0"), 10176)
        self.assertEqual(len(set(padding_offsets())), 636)
        for y, x in [(0, 0), (0, 101), (1, 0), (72, 0), (1, 97), (72, 101), (73, 101)]:
            offset = y * 15392 + x * 16
            self.assertEqual(expected[offset:offset+16], bytes(16))
        for y, x in [(0, 102), (1, 1), (72, 96), (73, 102), (74, 0)]:
            offset = y * 15392 + x * 16
            self.assertEqual(expected[offset:offset+16], b"\xa5" * 16)
        self.assertEqual(expected[-16:], b"\xa5" * 16)


if __name__ == "__main__":
    unittest.main()
