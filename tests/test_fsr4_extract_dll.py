# Copyright (C) 2026 BlackBearReloaded
# SPDX-License-Identifier: GPL-3.0-or-later
"""The DLL map and the pass interface describe every family consistently.

With the BC250 RC11 DLL under build/reference-runtime, every family is also extracted and
checked against the map; without it, that part is skipped.
"""
import hashlib
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import fsr4_extract_dll  # noqa: E402

DLL = ROOT / "build/reference-runtime/bc250-rc11/amd_fidelityfx_upscaler_dx12.dll"
MODEL_SHA256 = {"standard": "a54e552ff69a3f7199861417f7ae461d1f67f636ab532c6ec9d8ea5d2963735a",
                "ultra-performance": "3c42fc6eabfa9b5b1e489df0e9b2f58e3f9fa22048c584f6839bb27d32a0640e"}


class ExtractDll(unittest.TestCase):
    def test_map_covers_every_family(self):
        table = json.loads(fsr4_extract_dll.MAP.read_text())
        self.assertEqual(sorted(table["families"]), sorted(fsr4_extract_dll.FAMILIES))
        for name, family in table["families"].items():
            self.assertEqual(len(family["passes"]), 27, name)
            self.assertEqual(family["model"]["sha256"], MODEL_SHA256[name.rsplit("-band", 1)[0]], name)
            names = [p["family"] for p in family["passes"]]
            self.assertEqual(names[0], "prepass")
            self.assertEqual(names[-1], "postpass")

    def test_pass_interface(self):
        passes = json.loads((ROOT / "tools/fsr4_pass_abi.json").read_text())["passes"]
        self.assertEqual([p["index"] for p in passes], list(range(29)))
        rules = [p["rule"][0] for p in passes]
        self.assertEqual(rules[0], "SPD")
        self.assertEqual(rules[1], "PREPASS")
        self.assertEqual(rules[27], "POSTPASS")
        self.assertEqual(rules[28], "RCAS")
        self.assertEqual(set(rules[2:27:2]), {"PADDING"})
        self.assertEqual(set(rules[3:26:2]), {"NETWORK"})

    @unittest.skipUnless(DLL.is_file(), "the BC250 RC11 DLL is local")
    def test_extraction_matches_the_map(self):
        for family in fsr4_extract_dll.FAMILIES:
            shaders, model = fsr4_extract_dll.extract(DLL, family)
            self.assertEqual(sorted(shaders), list(range(29)), family)
            self.assertEqual(hashlib.sha256(model).hexdigest(), MODEL_SHA256[family.rsplit("-band", 1)[0]])


if __name__ == "__main__":
    unittest.main()
